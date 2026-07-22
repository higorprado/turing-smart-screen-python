# SPDX-License-Identifier: GPL-3.0-or-later
#
# turing-smart-screen-python - a Python system monitor and library for USB-C displays like Turing Smart Screen or XuanFang
# https://github.com/mathoudebine/turing-smart-screen-python/
#
# Copyright (C) 2021 Matthieu Houdebine (mathoudebine)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# This file will use Python libraries (psutil, GPUtil, etc.) to get hardware sensors
# For all platforms (Linux, Windows, macOS) but not all HW is supported

import math
import platform
import sys
import glob
import os
import ctypes
import struct
from collections import namedtuple, deque
from enum import IntEnum, auto
from typing import Tuple

# Nvidia GPU
import GPUtil
# CPU & disk sensors
import psutil

import library.sensors.sensors as sensors
from library.log import logger

# AMD GPU on Linux
try:
    import pyamdgpuinfo
except:
    pyamdgpuinfo = None

# AMD GPU on Windows
try:
    import pyadl
except:
    pyadl = None

PNIC_BEFORE = {}

# Auto-detect card location once
drm_path = "/sys/class/drm"
if not os.path.exists(drm_path):
    logger.error(f"{drm_path} does not exist! Cannot detect GPU cards.")
    card = None
else:
    # find first card directory
    card = next((f"card{i}" for i in range(10) if os.path.exists(os.path.join(drm_path, f"card{i}", "device"))), None)
    if card is None:
        logger.error(f"No GPU card directories found under {drm_path}!")
    else:
        logger.info(f"BC-250 APU detected at {card}")


# --- gamescope FPS via SysV message queue (mangoapp protocol) -------------
# gamescope publishes a mangoapp_msg_v1 frame on SysV MQ 0xffffffff at every
# presented frame. The mangoapp overlay is just a consumer of that queue; since
# we run without it, we are the sole reader. FPS = 1e9 / app_frametime_ns.
_IPC_NOWAIT = 0o4000
_MSG_NOERROR = 0o10000
_GAMESCOPE_MQ_KEY = 0xFFFFFFFF  # ftok("mangoapp", 65) fallback (gamescope PR #1660)

try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.msgget.argtypes = [ctypes.c_int, ctypes.c_int]
    _libc.msgget.restype = ctypes.c_int
    _libc.msgrcv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.c_long, ctypes.c_int]
    _libc.msgrcv.restype = ctypes.c_ssize_t
    _GAMESCOPE_IPC_AVAILABLE = True
except (OSError, AttributeError):
    _libc = None
    _GAMESCOPE_IPC_AVAILABLE = False


class _MangoappMsg(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("mtype", ctypes.c_long),                 # filled by msgrcv
        ("version", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("app_frametime_ns", ctypes.c_uint64),    # FPS source (validated by hexdump)
        ("fsrUpscale", ctypes.c_uint8),
        ("fsrSharpness", ctypes.c_uint8),
        ("visible_frametime_ns", ctypes.c_uint64),  # sentinel in this gamescope
        ("latency_ns", ctypes.c_uint64),
        ("outputWidth", ctypes.c_uint32),
        ("outputHeight", ctypes.c_uint32),
        ("displayRefresh", ctypes.c_uint16),
        ("flags", ctypes.c_uint8),
        ("engineName", ctypes.c_char * 40),
    ]


_gpu_fps_frametimes = deque(maxlen=120)  # ~1s window @120fps


def _drain_gamescope_queue() -> None:
    """Consume pending gamescope frame messages and refresh the FPS window."""
    if not _GAMESCOPE_IPC_AVAILABLE:
        return
    qid = _libc.msgget(_GAMESCOPE_MQ_KEY, 0o666)
    if qid < 0:
        ctypes.set_errno(0)
        return  # no gamescope running / queue absent: keep buffer as-is
    ctypes.set_errno(0)
    msg = _MangoappMsg()
    payload = ctypes.sizeof(_MangoappMsg) - ctypes.sizeof(ctypes.c_long)
    while True:
        n = _libc.msgrcv(qid, ctypes.byref(msg), payload, 1, _IPC_NOWAIT | _MSG_NOERROR)
        if n < 0:
            break
        ft = msg.app_frametime_ns
        if 0 < ft < 0xFFFFFFFFFFFFFFFF:
            _gpu_fps_frametimes.append(ft)


# --- gpu_metrics v2.x (Cyan Skillfish / AMD APU SMU) ---------------------
# Primary source for clocks/temps/power on this APU. Same data the amdgpu_top
# "GPU Metrics v2.2" widget reads. /proc/cpuinfo + psutil are unreliable here
# (no cpufreq driver -> stale/inflated values).
_GpuMetrics = namedtuple(
    "_GpuMetrics",
    ["cpu_coreclk", "current_gfxclk", "temperature_gfx", "temperature_soc", "gfx_activity"],
)


def _read_gpu_metrics():
    """Parse /sys/class/drm/<card>/device/gpu_metrics (v2.x). Returns a
    _GpuMetrics namedtuple or None if the file/format is unavailable."""
    if not card:
        return None
    path = f"/sys/class/drm/{card}/device/gpu_metrics"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if len(data) < 104:
        return None
    try:
        _structure_size, format_revision, _content_revision = struct.unpack_from("<HBB", data, 0)
    except Exception:
        return None
    if format_revision != 2:  # only v2.x layout is mapped here
        return None

    def u16(offset):
        v = struct.unpack_from("<H", data, offset)[0]
        return None if v == 0xFFFF else v

    def arr(offset, count):
        return [u16(offset + 2 * i) for i in range(count)]

    coreclk = [v for v in arr(88, 8) if v is not None]
    temp_gfx = u16(4)
    temp_soc = u16(6)
    return _GpuMetrics(
        cpu_coreclk=coreclk,
        current_gfxclk=u16(76),
        temperature_gfx=(temp_gfx / 100.0) if temp_gfx is not None else None,
        temperature_soc=(temp_soc / 100.0) if temp_soc is not None else None,
        gfx_activity=u16(28),
    )


class GpuType(IntEnum):
    UNSUPPORTED = auto()
    AMD = auto()
    NVIDIA = auto()


DETECTED_GPU = GpuType.UNSUPPORTED


# Function inspired of psutil/psutil/_pslinux.py:sensors_fans()
# Adapted to also get fan speed percentage instead of raw value
def sensors_fans():
    """Return hardware fans info (for CPU and other peripherals) as a
    dict including hardware label and current speed.

    Implementation notes:
    - /sys/class/hwmon looks like the most recent interface to
      retrieve this info, and this implementation relies on it
      only (old distros will probably use something else)
    - lm-sensors on Ubuntu 16.04 relies on /sys/class/hwmon
    """
    from psutil._common import bcat, cat
    import collections, glob, os

    ret = collections.defaultdict(list)
    basenames = glob.glob('/sys/class/hwmon/hwmon*/fan*_*')
    if not basenames:
        # CentOS has an intermediate /device directory:
        # https://github.com/giampaolo/psutil/issues/971
        basenames = glob.glob('/sys/class/hwmon/hwmon*/device/fan*_*')

    basenames = sorted(set([x.split('_')[0] for x in basenames]))
    for base in basenames:
        try:
            current_rpm = int(bcat(base + '_input'))

            try:
                max_rpm = int(bcat(base + '_max'))
            except:
                max_rpm = False  # Real maximum speed not found
            if not max_rpm:
                if current_rpm > 2200:
                    max_rpm = 3000  # AIO Pumps are usualy 3000 RPM
                elif current_rpm > 1500:
                    max_rpm = 2200  # High speed fans are usualy 2200 RPM
                else:
                    max_rpm = 1500  # Approximated: max fan speed is 1500 RPM

            try:
                min_rpm = int(bcat(base + '_min'))
            except:
                min_rpm = 0  # Approximated: min fan speed is 0 RPM
            percent = int((current_rpm - min_rpm) / (max_rpm - min_rpm) * 100)
        except (IOError, OSError) as err:
            continue
        unit_name = cat(os.path.join(os.path.dirname(base), 'name')).strip()
        label = cat(base + '_label', fallback=os.path.basename(base)).strip()

        custom_sfan = namedtuple('sfan', ['label', 'current', 'percent'])
        ret[unit_name].append(custom_sfan(label, current_rpm, percent))

    return dict(ret)


def is_cpu_fan(label: str) -> bool:
    return ("cpu" in label.lower()) or ("proc" in label.lower())


class Cpu(sensors.Cpu):
    @staticmethod
    def percentage(interval: float) -> float:
        try:
            return psutil.cpu_percent(interval=interval)
        except:
            return math.nan

    @staticmethod
    def frequency() -> float:
        # Prefer gpu_metrics current_coreclk (real CPU core clocks on this APU);
        # psutil.cpu_freq() falls back to /proc/cpuinfo, which is inflated/stale
        # here because there is no cpufreq driver.
        try:
            gm = _read_gpu_metrics()
            if gm and gm.cpu_coreclk:
                return sum(gm.cpu_coreclk) / len(gm.cpu_coreclk)
        except Exception:
            pass
        try:
            return psutil.cpu_freq().current
        except:
            return math.nan

    @staticmethod
    def load() -> Tuple[float, float, float]:  # 1 / 5 / 15min avg (%):
        try:
            return psutil.getloadavg()
        except:
            return math.nan, math.nan, math.nan

    @staticmethod
    def temperature() -> float:
        cpu_temp = math.nan
        try:
            sensors_temps = psutil.sensors_temperatures()
            if 'coretemp' in sensors_temps:
                # Intel CPU
                cpu_temp = sensors_temps['coretemp'][0].current
            elif 'k10temp' in sensors_temps:
                # AMD CPU
                cpu_temp = sensors_temps['k10temp'][0].current
            elif 'cpu_thermal' in sensors_temps:
                # ARM CPU
                cpu_temp = sensors_temps['cpu_thermal'][0].current
            elif 'zenpower' in sensors_temps:
                # AMD CPU with zenpower (k10temp is in blacklist)
                cpu_temp = sensors_temps['zenpower'][0].current
        except:
            # psutil.sensors_temperatures not available on Windows / MacOS
            pass
        return cpu_temp

    @staticmethod
    def fan_percent(fan_name: str = None) -> float:
        try:
            fans = sensors_fans()
            if fans:
                for name, entries in fans.items():
                    for entry in entries:
                        if fan_name is not None and fan_name == "%s/%s" % (name, entry.label):
                            # Manually selected fan
                            return entry.percent
                        elif is_cpu_fan(entry.label) or is_cpu_fan(name):
                            # Auto-detected fan
                            return entry.percent
        except:
            pass

        return math.nan


class Gpu(sensors.Gpu):
    @staticmethod
    def stats() -> Tuple[
        float, float, float, float, float]:  # load (%) / used mem (%) / used mem (Mb) / total mem (Mb) / temp (°C)
        if DETECTED_GPU == GpuType.AMD:
            return GpuAmd.stats()
        elif DETECTED_GPU == GpuType.NVIDIA:
            return GpuNvidia.stats()
        else:
            return math.nan, math.nan, math.nan, math.nan, math.nan

    @staticmethod
    def fps() -> int:
        if DETECTED_GPU == GpuType.AMD:
            return GpuAmd.fps()
        elif DETECTED_GPU == GpuType.NVIDIA:
            return GpuNvidia.fps()
        else:
            return -1

    @staticmethod
    def fan_percent() -> float:
        if DETECTED_GPU == GpuType.AMD:
            return GpuAmd.fan_percent()
        elif DETECTED_GPU == GpuType.NVIDIA:
            return GpuNvidia.fan_percent()
        else:
            return math.nan

    @staticmethod
    def frequency() -> float:
        if DETECTED_GPU == GpuType.AMD:
            return GpuAmd.frequency()
        elif DETECTED_GPU == GpuType.NVIDIA:
            return GpuNvidia.frequency()
        else:
            return math.nan

    @staticmethod
    def is_available() -> bool:
        global DETECTED_GPU
        # Always use Nvidia GPU if available
        if GpuNvidia.is_available():
            logger.info("Detected Nvidia GPU(s)")
            DETECTED_GPU = GpuType.NVIDIA
        # Otherwise, use the AMD GPU / APU if available
        elif GpuAmd.is_available():
            logger.info("Detected AMD GPU(s)")
            DETECTED_GPU = GpuType.AMD
        else:
            logger.warning("No supported GPU found")
            DETECTED_GPU = GpuType.UNSUPPORTED
            if sys.version_info >= (3, 11) and (platform.system() == "Linux" or platform.system() == "Darwin"):
                logger.warning("If you have an AMD GPU, you may need to install some  libraries manually: see "
                               "https://github.com/mathoudebine/turing-smart-screen-python/wiki/Troubleshooting#linux--macos-no-supported-gpu-found-with-an-amd-gpu-and-python-311")

        return DETECTED_GPU != GpuType.UNSUPPORTED


class GpuNvidia(sensors.Gpu):
    @staticmethod
    def stats() -> Tuple[
        float, float, float, float, float]:  # load (%) / used mem (%) / used mem (Mb) / total mem (Mb) / temp (°C)
        # Unlike other sensors, Nvidia GPU with GPUtil pulls in all the stats at once
        nvidia_gpus = GPUtil.getGPUs()

        try:
            memory_used_all = [item.memoryUsed for item in nvidia_gpus]
            memory_used_mb = sum(memory_used_all) / len(memory_used_all)
        except:
            memory_used_mb = math.nan

        try:
            memory_total_all = [item.memoryTotal for item in nvidia_gpus]
            memory_total_mb = sum(memory_total_all) / len(memory_total_all)
        except:
            memory_total_mb = math.nan

        try:
            memory_percentage = (memory_used_mb / memory_total_mb) * 100
        except:
            memory_percentage = math.nan

        try:
            load_all = [item.load for item in nvidia_gpus]
            load = (sum(load_all) / len(load_all)) * 100
        except:
            load = math.nan

        try:
            temperature_all = [item.temperature for item in nvidia_gpus]
            temperature = sum(temperature_all) / len(temperature_all)
        except:
            temperature = math.nan

        return load, memory_percentage, memory_used_mb, memory_total_mb, temperature

    @staticmethod
    def fps() -> int:
        # Not supported by Python libraries
        return -1

    @staticmethod
    def fan_percent() -> float:
        try:
            fans = sensors_fans()
            if fans:
                for name, entries in fans.items():
                    for entry in entries:
                        if "gpu" in (entry.label.lower() or name.lower()):
                            return entry.percent
        except:
            pass

        return math.nan

    @staticmethod
    def frequency() -> float:
        # Not supported by Python libraries
        return math.nan

    @staticmethod
    def is_available() -> bool:
        try:
            return len(GPUtil.getGPUs()) > 0
        except:
            return False



class GpuAmd(sensors.Gpu):

    def get_gpu_load():
        """
        Read GPU load (%) from patched or unpatched gpu_metrics.

        Handles:
        - Old Cyan Skillfish: 1-100 raw values
        - New Cyan Skillfish: 0-10000 raw values (decimal scaling)
        - Unpatched / placeholder: 65535 -> 0

        Returns integer (0-100) for Turzx compatibility.
        """
        try:
            with open(f"/sys/class/drm/{card}/device/gpu_metrics", "rb") as f:
                data = f.read(128)

            raw_load = int.from_bytes(data[28:30], byteorder="little")

            # Null / invalid
            if raw_load == 65535:
                return 0

            # Detect new patch: raw > 100 → scale down from 0-10000 to 0-100
            if raw_load > 100:
                load = raw_load / 100
            else:
                # old patch: use as-is
                load = raw_load

            # Cap at 100% just in case
            if load > 100:
                load = 100


            return int(load)

        except Exception as e:
            # fallback 0
            logger.warning(f"GPU load read error: {e}")
            return 0

    @staticmethod
    def stats() -> Tuple[
        float, float, float, float, float]:  # load (%) / used mem (%) / used mem (Mb) / total mem (Mb) / temp


        clock = temp = 0
        used_gb = 0.0
        try:
            clock_path = f"/sys/class/drm/{card}/device/pp_dpm_sclk"
            if os.path.exists(clock_path):
                with open(clock_path) as f:
                    for line in f:
                        if "*" in line:
                            parts = line.strip().split(":")[-1].strip().split("Mhz")[0]
                            clock = int(parts)
                            break

            temp_path = f"/sys/class/drm/{card}/device/hwmon"
            for entry in os.listdir(temp_path):
                sensor_path = os.path.join(temp_path, entry, "temp1_input")
                if os.path.exists(sensor_path):
                    with open(sensor_path) as f:
                        temp = int(f.read()) / 1000
                        break

            try:
                vram_used = int(open(f"/sys/class/drm/{card}/device/mem_info_vram_used").read().strip())
                gtt_used  = int(open(f"/sys/class/drm/{card}/device/mem_info_gtt_used").read().strip())
                used_gb = round((vram_used + gtt_used) / (1024**3), 2)
            except Exception as e:
                logger.warning(f"VRAM sysfs parse error: {e}")
        except Exception as e:
            logger.warning(f"GPU stats error: {e}")
        return GpuAmd.get_gpu_load(), used_gb/16, used_gb*1024, 16*1024, temp


    @staticmethod
    def fps() -> int:
        _drain_gamescope_queue()
        if not _gpu_fps_frametimes:
            return 0
        mean_ft = sum(_gpu_fps_frametimes) / len(_gpu_fps_frametimes)
        if mean_ft <= 0:
            return 0
        return int(round(1_000_000_000 / mean_ft))

    @staticmethod
    def fan_percent() -> float:
        try:
            # Try with psutil fans
            fans = sensors_fans()
            if fans:
                for name, entries in fans.items():
                    for entry in entries:
                        if "gpu" in (entry.label.lower() or name.lower()):
                            return entry.percent

            # Try with pyadl if psutil did not find GPU fan
            if pyadl:
                return pyadl.ADLManager.getInstance().getDevices()[0].getCurrentFanSpeed(
                    pyadl.ADL_DEVICE_FAN_SPEED_TYPE_PERCENTAGE)
        except:
            pass

        return math.nan

    @staticmethod
    def frequency() -> float:
        # gpu_metrics current_gfxclk is the primary, accurate source on this APU.
        # (nvidia-smi is not applicable to AMD and is intentionally not used.)
        try:
            gm = _read_gpu_metrics()
            if gm and gm.current_gfxclk is not None:
                return float(gm.current_gfxclk)
        except Exception:
            pass
        try:
            import re as _re
            for f in glob.glob('/sys/class/drm/card[0-9]/device/pp_dpm_sclk'):
                for line in open(f):
                    if '*' in line:   # linha do clock ATIVO no momento
                        m = _re.search(r'(\d+)\s*[Mm]hz', line)

                        if m:
                            return int(m.group(1))
        except Exception:
            pass
        return None

    @staticmethod
    def is_available() -> bool:
        # External libs (kept for non-BC250 setups / Windows)
        try:
            if pyamdgpuinfo and pyamdgpuinfo.detect_gpus() > 0:
                return True
            if pyadl and len(pyadl.ADLManager.getInstance().getDevices()) > 0:
                return True
        except:
            pass
        # BC-250 / AMD APU via sysfs: global `card` already located a drm card
        # with a device. All GpuAmd readers use sysfs (gpu_metrics/hwmon/
        # pp_dpm_sclk), not pyamdgpuinfo, so confirm the driver is amdgpu.
        try:
            if card:
                with open(f"/sys/class/drm/{card}/device/uevent") as f:
                    if "DRIVER=amdgpu" in f.read():
                        return True
        except:
            pass
        return False


class Memory(sensors.Memory):


    @staticmethod
    def swap_percent() -> float:
        try:
            return psutil.swap_memory().percent
        except:
            return math.nan

    @staticmethod
    def virtual_percent() -> float:
        try:
            return Memory.virtual_used()/(Memory.virtual_used()+Memory.virtual_free())*100
        except:
            return -1

    @staticmethod
    def virtual_used() -> int:  # In bytes
        try:
            # Do not use psutil.virtual_memory().used: from https://psutil.readthedocs.io/en/latest/#memory
            # "It is calculated differently depending on the platform and designed for informational purposes only"
            return psutil.virtual_memory().total - psutil.virtual_memory().available
        except:
            return -1

    @staticmethod
    def virtual_free() -> int:  # In bytes
        try:
            # Do not use psutil.virtual_memory().free: from https://psutil.readthedocs.io/en/latest/#memory
            # "note that this doesn’t reflect the actual memory available (use available instead)."
            return psutil.virtual_memory().available
        except:
            return -1


class Disk(sensors.Disk):
    @staticmethod
    def disk_usage_percent() -> float:
        try:
            return psutil.disk_usage("/").percent
        except:
            return math.nan

    @staticmethod
    def disk_used() -> int:  # In bytes
        try:
            return psutil.disk_usage("/").used
        except:
            return -1

    @staticmethod
    def disk_free() -> int:  # In bytes
        try:
            return psutil.disk_usage("/").free
        except:
            return -1


class Net(sensors.Net):
    @staticmethod
    def stats(if_name, interval) -> Tuple[
        int, int, int, int]:  # up rate (B/s), uploaded (B), dl rate (B/s), downloaded (B)
        try:
            # Get current counters
            pnic_after = psutil.net_io_counters(pernic=True)

            upload_rate = 0
            uploaded = 0
            download_rate = 0
            downloaded = 0

            if if_name != "":
                if if_name in pnic_after:
                    try:
                        upload_rate = (pnic_after[if_name].bytes_sent - PNIC_BEFORE[if_name].bytes_sent) / interval
                        uploaded = pnic_after[if_name].bytes_sent
                        download_rate = (pnic_after[if_name].bytes_recv - PNIC_BEFORE[if_name].bytes_recv) / interval
                        downloaded = pnic_after[if_name].bytes_recv
                    except:
                        # Interface might not be in PNIC_BEFORE for now
                        pass

                    PNIC_BEFORE.update({if_name: pnic_after[if_name]})
                else:
                    logger.warning("Network interface '%s' not found. Check names in config.yaml." % if_name)

            return upload_rate, uploaded, download_rate, downloaded
        except:
            return -1, -1, -1, -1
