# -*- coding: utf-8 -*-
"""
turzx_display.py  -  Driver/tema para a Turing Smart Screen 3.5" (Rev A, USB35INCHIPSV2)
Versao WINDOWS (para visualizar o tema na tela real antes de portar ao ZimaOS).

- Protocolo Turing Rev A confirmado por captura USB (init replicado byte a byte).
- Tema "pink plexus" reduzido de 5" (800x480) para 3.5" (480x320), painel de dados a direita.
- Fundo enviado 1x; depois so os retangulos dinamicos (%, barras, relogio, temps) sao reenviados.

Uso:
  python turzx_display.py            # roda em loop (fecha o UsbMonitor.exe antes!)
  python turzx_display.py --bg       # so envia o fundo e sai
  python turzx_display.py --test     # padrao de cor (verifica ordem RGB565)
  python turzx_display.py --le        # forca RGB565 little-endian (se as cores sairem trocadas)
  python turzx_display.py --port COM6
"""
import sys, os, time, argparse, subprocess, platform, glob

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("Falta pyserial:  pip install pyserial")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Falta Pillow:  pip install Pillow")
try:
    import numpy as np
except ImportError:
    sys.exit("Falta numpy:  pip install numpy")
try:
    import psutil
except ImportError:
    sys.exit("Falta psutil:  pip install psutil")

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, 'assets')
W, H = 480, 320
USE_PORTRAIT = False   # --portrait: layout vertical 320x480 (BC-250)

# ---------------------------------------------------------------- TELA
# Usa a lib de referencia smartscreen_driver (Rev A), que faz o protocolo serial
# corretamente no Linux (rtscts + envio dos pixels em pedacos). Reimplementar isso
# na mao corrompia o bitmap grande no Linux; a lib resolve.
try:
    from smartscreen_driver.lcd_comm_rev_a import LcdCommRevA
    from smartscreen_driver.lcd_comm import Orientation
except ImportError:
    sys.exit("Falta smartscreen-driver:  pip install smartscreen-driver")


class Screen:
    def __init__(self, port, big_endian=False):
        # painel nativo 320x480 (portrait); em landscape vira 480x320.
        self.lcd = LcdCommRevA(com_port=port, display_width=320, display_height=480)

    def init(self):
        self.lcd.initialize_comm()
        self.lcd.screen_on()
        self.lcd.set_brightness(100)
        self.lcd.set_orientation(Orientation.PORTRAIT if USE_PORTRAIT else Orientation.LANDSCAPE)

    def blit(self, img, x, y):
        self.lcd.paint(img, pos=(x, y))

    def close(self):
        try:
            self.lcd.close_serial()
        except Exception:
            pass


def find_port(prefer=None):
    if prefer:
        return prefer
    for p in list_ports.comports():
        hw = (p.hwid or '').upper()
        prod = (getattr(p, 'product', '') or '').upper()
        if ('1A86' in hw and '5722' in hw) or 'USB35INCH' in hw or 'USB35INCH' in prod:
            return p.device
    return None


def wait_for_port(prefer=None):
    """Bloqueia ate a tela aparecer em qualquer porta USB (detecta pelo VID/PID)."""
    announced = False
    while True:
        p = find_port(prefer)
        if p:
            return p
        if not announced:
            print("aguardando a tela ser plugada na USB...")
            announced = True
        time.sleep(2)

# ---------------------------------------------------------------- FONTES
def _font(names, size):
    for n in names:
        for base in (os.path.join(ASSETS, 'fonts'), r'C:\Windows\Fonts'):
            p = os.path.join(base, n)
            if os.path.exists(p):
                try: return ImageFont.truetype(p, size)
                except Exception: pass
    return ImageFont.load_default()

F_CODE  = _font(['bahnschrift.ttf', 'segoeuib.ttf', 'arialbd.ttf'], 26)
F_FULL  = _font(['bahnschrift.ttf', 'segoeui.ttf'], 12)
F_DEV   = _font(['bahnschrift.ttf', 'segoeui.ttf'], 13)
F_PCT   = _font(['bahnschrift.ttf', 'segoeuib.ttf', 'arialbd.ttf'], 28)
F_CLK   = _font(['bahnschrift.ttf', 'segoeuib.ttf'], 34)
F_DATE  = _font(['bahnschrift.ttf', 'segoeui.ttf'], 14)
F_HDATE = _font(['bahnschrift.ttf', 'segoeuib.ttf', 'segoeui.ttf'], 17)
F_H     = _font(['bahnschrift.ttf', 'segoeuib.ttf'], 13)
F_SM    = _font(['bahnschrift.ttf', 'segoeui.ttf'], 13)
F_SSDI  = _font(['bahnschrift.ttf', 'segoeuib.ttf', 'segoeui.ttf'], 16)
F_TEMP  = _font(['bahnschrift.ttf', 'segoeuib.ttf', 'segoeui.ttf'], 24)
F_TINY  = _font(['bahnschrift.ttf', 'segoeui.ttf'], 10)


def _brand(size, weight='Black'):
    """Fonte de marca (Orbitron, sci-fi). Cai pro Bahnschrift se nao existir."""
    for base in (os.path.join(ASSETS, 'fonts'), r'C:\Windows\Fonts'):
        p = os.path.join(base, 'Orbitron.ttf')
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                try:
                    f.set_variation_by_name(weight)
                except Exception:
                    pass
                return f
            except Exception:
                pass
    return _font(['bahnschrift.ttf', 'segoeuib.ttf'], size)

F_BRAND = _brand(18, 'Black')

PURPLE = (138, 43, 214); WHITE = (240, 238, 245); GRAY = (176, 170, 190); CYAN = (120, 210, 255)

# ---------------------------------------------------------------- SENSORES (Windows)
class Sensors:
    def __init__(self):
        self._gpu_ok = self._has_nvidia_smi()
        psutil.cpu_percent()  # priming

    @staticmethod
    def _has_nvidia_smi():
        try:
            subprocess.run(['nvidia-smi', '-L'], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def cpu_name(self):
        if platform.system() != 'Windows':
            try:
                for line in open('/proc/cpuinfo'):
                    if line.lower().startswith('model name'):
                        v = line.split(':', 1)[1].split('@')[0]   # tira " @ 2.60GHz"
                        v = v.replace('(R)', '').replace('(TM)', '').replace('CPU', '')
                        return ' '.join(v.split())
            except Exception:
                pass
            return platform.processor() or "CPU"
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            v = winreg.QueryValueEx(k, "ProcessorNameString")[0].split('@')[0]
            return ' '.join(v.replace('(R)', '').replace('(TM)', '').replace('CPU', '').split())
        except Exception:
            return platform.processor() or "CPU"

    def cpu_pct(self):
        return psutil.cpu_percent()

    def cpu_clock(self):
        # retorna MHz (sem teto; valor atual real)
        try:
            f = psutil.cpu_freq()
            if f and f.current:
                return f.current
        except Exception:
            pass
        try:
            for line in open('/proc/cpuinfo'):
                if line.lower().startswith('cpu mhz'):
                    return float(line.split(':')[1])
        except Exception:
            pass
        return None

    def cpu_temp(self):
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            temps = {}
        for key in ('coretemp', 'k10temp', 'zenpower', 'cpu_thermal', 'acpitz'):
            arr = temps.get(key)
            if arr:
                for e in arr:
                    lbl = (e.label or '').lower()
                    if 'package' in lbl or 'tctl' in lbl:
                        return e.current
                return arr[0].current
        for arr in temps.values():
            if arr:
                return arr[0].current
        try:
            for z in sorted(glob.glob('/sys/class/thermal/thermal_zone*/temp')):
                return int(open(z).read().strip()) / 1000.0
        except Exception:
            pass
        return None

    def ram(self):
        vm = psutil.virtual_memory()
        return vm.used / (1024 ** 3), vm.total / (1024 ** 3), vm.percent

    def mem_label(self):
        gb = round(psutil.virtual_memory().total / (1024 ** 3))
        typ = 'DDR4'
        if platform.system() != 'Windows':
            typ = 'RAM'
            try:
                out = subprocess.run(['dmidecode', '-t', '17'], capture_output=True,
                                     text=True, timeout=5).stdout
                for l in out.splitlines():
                    s = l.strip()
                    if s.startswith('Type:') and 'Unknown' not in s:
                        v = s.split(':', 1)[1].strip()
                        if v.startswith(('DDR', 'LPDDR')):
                            typ = v
                            break
            except Exception:
                pass
        return f"{typ} {gb} GB"

    def gpus(self):
        g = self._nvidia_gpus()
        if g:
            return g
        return self._amd_gpus()   # APUs AMD (ex.: BC-250) via sysfs

    def _nvidia_gpus(self):
        if not self._gpu_ok:
            return []
        try:
            q = 'name,utilization.gpu,memory.used,memory.total,temperature.gpu'
            out = subprocess.run(['nvidia-smi', f'--query-gpu={q}',
                                  '--format=csv,noheader,nounits'],
                                 capture_output=True, text=True, timeout=5).stdout
            res = []
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 5:
                    name, use, mu, mt, temp = parts[:5]
                    name = name.replace('NVIDIA', '').replace('GeForce', '').strip()
                    res.append(dict(name=name, use=float(use),
                                    vram_used=float(mu) / 1024, vram_total=float(mt) / 1024,
                                    temp=float(temp)))
            return res
        except Exception:
            return []

    def _amd_gpus(self):
        """GPU AMD via sysfs (/sys/class/drm) — APUs como a BC-250, sem nvidia-smi/rocm-smi."""
        if platform.system() == 'Windows':
            return []
        def rd(path, cast=float, default=None):
            try:
                return cast(open(path).read().strip())
            except Exception:
                return default
        res = []
        for card in sorted(glob.glob('/sys/class/drm/card[0-9]')):
            dev = os.path.join(card, 'device')
            if not os.path.exists(os.path.join(dev, 'gpu_busy_percent')):
                continue
            use = rd(os.path.join(dev, 'gpu_busy_percent'), float, 0.0)
            vu = (rd(os.path.join(dev, 'mem_info_vram_used'), int, 0) or 0) / (1024 ** 3)
            vt = (rd(os.path.join(dev, 'mem_info_vram_total'), int, 0) or 0) / (1024 ** 3)
            temp = None
            for hw in sorted(glob.glob(os.path.join(dev, 'hwmon', 'hwmon*'))):
                t = rd(os.path.join(hw, 'temp1_input'), int, None)
                if t is not None:
                    temp = t / 1000.0
                    break
            res.append(dict(name=self._amd_name(dev), use=use,
                            vram_used=vu, vram_total=vt, temp=temp or 0))
        return res

    @staticmethod
    def _amd_name(dev):
        try:
            slot = os.path.basename(os.path.realpath(dev))
            out = subprocess.run(['lspci', '-s', slot], capture_output=True, text=True, timeout=3).stdout
            if ':' in out:
                nm = out.split(':', 2)[-1].replace('Advanced Micro Devices, Inc.', 'AMD')
                nm = nm.replace('[AMD/ATI]', '').replace('VGA compatible controller', '').strip()
                return (' '.join(nm.split())[:22]) or "AMD GPU"
        except Exception:
            pass
        return "AMD GPU"

    def ssd_pct(self):
        try:
            return psutil.disk_usage('/').percent
        except Exception:
            return 0.0

    def ssd_space(self):
        # % de espaco usado do maior filesystem real (o SSD principal)
        best = None
        try:
            for p in psutil.disk_partitions(all=False):
                if p.fstype.lower() in ('', 'tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'efivarfs'):
                    continue
                try:
                    u = psutil.disk_usage(p.mountpoint)
                except Exception:
                    continue
                if u.total < 50 * (1024 ** 3):
                    continue
                if best is None or u.total > best[1]:
                    best = (u.percent, u.total)
        except Exception:
            pass
        return best[0] if best else 0.0

    def ssd_space_info(self):
        # (% usado, livre_GB, total_GB) do maior filesystem real (SSD principal)
        best = None
        try:
            for p in psutil.disk_partitions(all=False):
                if p.fstype.lower() in ('', 'tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'efivarfs'):
                    continue
                try:
                    u = psutil.disk_usage(p.mountpoint)
                except Exception:
                    continue
                if u.total < 50 * (1024 ** 3):
                    continue
                if best is None or u.total > best[-1]:
                    best = (u.percent, u.free / 1e9, u.total / 1e9, u.total)
        except Exception:
            pass
        return (best[0], best[1], best[2]) if best else (0.0, 0.0, 0.0)

    def _main_block_dev(self):
        d = getattr(self, '_mbd', None)
        if d:
            return d
        try:
            for name in sorted(os.listdir('/sys/block')):
                if name.startswith(('loop', 'ram', 'zram', 'sr')):
                    continue
                self._mbd = name
                return name
        except Exception:
            pass
        return None

    def ssd_busy(self):
        # %util do disco (tempo gasto em I/O, estilo iostat) = uso momentaneo real
        dev = self._main_block_dev()
        if not dev:
            return 0.0
        try:
            io_ticks = int(open(f'/sys/block/{dev}/stat').read().split()[9])
        except Exception:
            return 0.0
        import time as _t
        now = _t.monotonic()
        prev = getattr(self, '_io_prev', None)
        self._io_prev = (now, io_ticks)
        if not prev:
            return 0.0
        dt = now - prev[0]
        if dt <= 0:
            return 0.0
        return max(0.0, min(100.0, (io_ticks - prev[1]) / (dt * 1000.0) * 100.0))

    def mem_avail(self):
        vm = psutil.virtual_memory()
        return vm.available / (1024 ** 3), vm.total / (1024 ** 3)

    def gpu_clock(self):
        # retorna MHz (sem teto; valor atual real, acompanha overclock)
        if self._gpu_ok:
            try:
                out = subprocess.run(['nvidia-smi', '--query-gpu=clocks.gr',
                                      '--format=csv,noheader,nounits'],
                                     capture_output=True, text=True, timeout=5).stdout
                v = out.strip().splitlines()
                if v:
                    return float(v[0])
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

    def mem_temp(self):
        # temp da memoria (GDDR6 unificada da BC-250): hwmon do amdgpu com label 'mem'
        for dev in glob.glob('/sys/class/drm/card[0-9]/device'):
            for hw in glob.glob(dev + '/hwmon/hwmon*'):
                for lbl in glob.glob(hw + '/temp*_label'):
                    try:
                        if 'mem' in open(lbl).read().strip().lower():
                            return int(open(lbl.replace('_label', '_input')).read().strip()) / 1000.0
                    except Exception:
                        pass
        return None

    def ssd_temp(self):
        for (_, t, _) in self.disks():
            if t is not None:
                return t
        return None

    def games_disk(self):
        """Maior volume montado que nao seja '/' (o SSD de jogos da BC-250). (pct, path, total_gb)."""
        best = None
        try:
            for p in psutil.disk_partitions(all=False):
                if p.mountpoint == '/' or p.fstype.lower() in ('', 'tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'efivarfs'):
                    continue
                try:
                    u = psutil.disk_usage(p.mountpoint)
                except Exception:
                    continue
                if u.total < 100 * (1024 ** 3):
                    continue
                if best is None or u.total > best[3]:
                    best = (u.percent, p.mountpoint, u.total / (1024 ** 3), u.total)
        except Exception:
            pass
        return (best[0], best[1], best[2]) if best else None

    def disks(self):
        """Retorna lista (label, temp_C|None, size_gb)."""
        if platform.system() != 'Windows':
            return self._linux_disks()
        res = []
        # tamanho total dos discos fisicos via psutil (particoes)
        seen = {}
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                seen[part.device[:2]] = u.total / 1e9
            except Exception:
                pass
        # temperatura: tenta smartctl se existir
        temps = self._smart_temps()
        if temps:
            for name, t, sz in temps:
                res.append((name, t, sz))
        else:
            for dev, sz in seen.items():
                res.append((dev, None, sz))
        return res

    @staticmethod
    def _smart_temps():
        try:
            out = subprocess.run(['smartctl', '--scan'], capture_output=True, text=True, timeout=5).stdout
            disks = []
            for line in out.splitlines():
                dev = line.split()[0] if line.strip() else None
                if dev:
                    info = subprocess.run(['smartctl', '-A', '-i', dev],
                                          capture_output=True, text=True, timeout=8).stdout
                    t = None; sz = 0
                    for l in info.splitlines():
                        ll = l.lower()
                        if 'temperature' in ll:
                            for tok in l.split():
                                if tok.isdigit():
                                    t = float(tok); break
                        if 'user capacity' in ll or 'total nvm capacity' in ll:
                            digits = ''.join(c for c in l.split('[')[0] if c.isdigit())
                            if digits: sz = int(digits) / 1e9
                    disks.append((os.path.basename(dev), t, sz))
            return disks
        except Exception:
            return []

    def _linux_disks(self):
        res = []
        try:
            out = subprocess.run(['lsblk', '-dnbo', 'NAME,SIZE,TYPE'],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            out = ''
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 3 and p[2] == 'disk':
                name, sizeb = p[0], int(p[1])
                if name.startswith(('loop', 'ram', 'zram')):
                    continue
                res.append((name, self._smart_temp_linux('/dev/' + name), sizeb / 1e9))
        return res

    def _smart_temp_linux(self, dev):
        import json
        for extra in ([], ['-d', 'sat'], ['-d', 'nvme']):
            try:
                out = subprocess.run(['smartctl', '-j', '-A'] + extra + [dev],
                                     capture_output=True, text=True, timeout=8).stdout
                t = json.loads(out).get('temperature', {}).get('current')
                if t:
                    return float(t)
            except Exception:
                pass
        return self._sysfs_disk_temp(os.path.basename(dev))

    @staticmethod
    def _sysfs_disk_temp(name):
        """Temp de disco sem smartctl: NVMe expoe hwmon; SATA via modulo drivetemp."""
        for pat in (f'/sys/block/{name}/device/hwmon/hwmon*/temp1_input',
                    f'/sys/block/{name}/device/device/hwmon/hwmon*/temp1_input'):
            for ti in glob.glob(pat):
                try:
                    return int(open(ti).read().strip()) / 1000.0
                except Exception:
                    pass
        return None


class DemoSensors(Sensors):
    """Dados representativos do box ZimaOS (Xeon E5-2697A v4, 64GB, 2x RTX 3060, varios HDs)
    para validar o layout completo nesta tela, ja que o PC de teste nao tem GPU/smartctl."""
    def __init__(self):
        import random
        self._rnd = random
        self._gpu_ok = True

    def cpu_name(self):
        return "Intel Xeon E5-2697A v4"

    def cpu_pct(self):
        return self._rnd.randint(6, 45)

    def cpu_clock(self):
        return self._rnd.randint(2300, 2600)

    def cpu_temp(self):
        return self._rnd.randint(44, 58)

    def ram(self):
        return 21.4, 64.0, 33.0

    def mem_label(self):
        return "DDR4 64 GB"

    def gpus(self):
        j = self._rnd.randint(-3, 3)
        return [dict(name='RTX 3060', use=37 + j, vram_used=5.2, vram_total=12.0, temp=54 + j),
                dict(name='RTX 3060', use=12 + j, vram_used=1.1, vram_total=12.0, temp=47 + j)]

    def disks(self):
        return [('sda', 38, 4000), ('sdb', 41, 4000), ('sdc', 44, 4000)]

    def ssd_pct(self):
        return 73

    def ssd_busy(self):
        return self._rnd.randint(6, 42)

    def ssd_space(self):
        return 93

    def ssd_space_info(self):
        return 93.0, 140.0, 2000.0   # 93% usado, 140 GB livres, 2 TB total

    def gpu_clock(self):
        return self._rnd.randint(2200, 2500)

    def mem_avail(self):
        if USE_PORTRAIT:
            return 12.4, 15.4   # BC-250: psutil reporta ~15.4 numa de 16 GB (usado ~3)
        return 42.6, 64.0

    def mem_temp(self):
        return self._rnd.randint(58, 70)

    def ssd_temp(self):
        return self._rnd.randint(38, 46)

    def games_disk(self):
        return (31, '/var/mnt/games', 2000.0)

# ---------------------------------------------------------------- TEMA / RENDER
def br_now():
    import datetime
    # UTC-3 fixo (Brasilia, sem horario de verao), timezone-aware
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))

DIAS = ['SEG', 'TER', 'QUA', 'QUI', 'SEX', 'SAB', 'DOM']

def nice_mem(g):
    """Encaixa o total de RAM no tamanho fisico padrao mais proximo.
    O SO reserva parte da memoria, entao o psutil reporta menos (ex.: 15.4 numa de 16 GB)."""
    for s in (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256):
        if g <= s + 0.5:
            return s
    return round(g)

class Theme:
    PX = 256  # x do painel direito

    def __init__(self, sensors):
        self.s = sensors
        self.cpu_name = self.s.cpu_name()
        gs = self.s.gpus()
        self.gpu_names = [g['name'] for g in gs] or ['GPU']
        self.ram_label = self.s.mem_label()
        self.base = self._render_base()
        self._last = {}

    # ---- fundo + elementos estaticos ----
    def _bg(self):
        p = os.path.join(ASSETS, 'background_480x320.png')
        if os.path.exists(p):
            return Image.open(p).convert('RGB').resize((W, H), Image.LANCZOS)
        return Image.new('RGB', (W, H), (12, 8, 20))

    def _header_bar(self, d, x, y, code, full):
        sk = 10
        d.polygon([(x, y), (x + 232, y), (x + 232 - sk, y + 22), (x - sk, y + 22)], fill=PURPLE)
        d.polygon([(x, y), (x + 6, y), (x + 6 - sk, y + 22), (x - sk, y + 22)], fill=(255, 255, 255))
        d.text((x + 10, y + 1), code, font=F_CODE, fill=WHITE)
        cw = d.textlength(code, font=F_CODE)
        d.text((x + 18 + cw, y + 7), full, font=F_FULL, fill=(235, 225, 255))

    def _render_base(self):
        img = self._bg()
        d = ImageDraw.Draw(img, 'RGBA')
        # blocos esquerda (parte estatica)
        blocks = [(14, 'CPU', 'CENTRAL PROCESSING UNIT', self.cpu_name),
                  (115, 'GPU', 'GRAPHICS CARD', self.gpu_names[0]),
                  (216, 'RAM', 'COMPUTER MEMORY', self.ram_label)]
        for y, code, full, dev in blocks:
            self._header_bar(d, 10, y, code, full)
            d.text((12, y + 26), dev, font=F_DEV, fill=GRAY)
        # painel direito
        d.rounded_rectangle([self.PX, 8, 474, 312], radius=10, fill=(18, 12, 30, 150))
        d.text((self.PX + 150, 18), "UTC-3", font=F_TINY, fill=GRAY)
        # linha CPU (clock + temp + RAM usada) e' dinamica, desenhada em y=70
        d.line([(self.PX + 12, 92), (468, 92)], fill=(120, 90, 180, 160), width=1)
        d.text((self.PX + 14, 98), "GPU  VRAM / TEMP", font=F_H, fill=(200, 170, 255))
        yy = 116
        for nm in self.gpu_names[:2]:
            d.text((self.PX + 14, yy), nm, font=F_SM, fill=WHITE)
            yy += 38
        d.line([(self.PX + 12, yy), (468, yy)], fill=(120, 90, 180, 160), width=1)
        d.text((self.PX + 14, yy + 8), "ARMAZENAMENTO", font=F_H, fill=(200, 170, 255))
        self._stor_y = yy + 8
        return img

    # ---- helpers de campo dinamico ----
    def _crop(self, rect):
        return self.base.crop(rect).copy()

    def _progress(self, im, ox, oy, w, h, pct):
        d = ImageDraw.Draw(im, 'RGBA')
        r = h // 2
        d.rounded_rectangle([ox, oy, ox + w, oy + h], radius=r, fill=(30, 22, 45, 220))
        fw = int(w * pct / 100)
        if fw > h:
            grad = Image.new('RGB', (fw, h))
            ga = np.zeros((h, fw, 3), dtype=np.uint8)
            t = np.linspace(0, 1, fw)
            ga[:, :, 0] = (150 + 90 * t).astype(np.uint8)
            ga[:, :, 1] = (120 + 110 * t).astype(np.uint8)
            ga[:, :, 2] = (220 + 30 * t).astype(np.uint8)
            grad = Image.fromarray(ga)
            mask = Image.new('L', (fw, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw - 1, h - 1], radius=r, fill=255)
            im.paste(grad, (ox, oy), mask)
            d.ellipse([ox + fw - h, oy, ox + fw, oy + h], fill=WHITE)

    # ---- campos dinamicos: cada um devolve (rect, imagem) ----
    def field_block_value(self, y, pct):
        """Bloco esquerda: % grande + barra. rect cobre os dois."""
        rect = (12, y + 44, 244, y + 70)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        d.text((0, 0), f"{int(round(pct))}%", font=F_PCT, fill=WHITE)
        self._progress(im, 58, 10, 160, 12, pct)
        return rect, im

    def field_clock(self):
        now = br_now()
        rect = (self.PX + 12, 12, 472, 70)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        d.text((2, 2), now.strftime('%H:%M:%S'), font=F_CLK, fill=WHITE)
        d.text((4, 40), f"{DIAS[now.weekday()]} {now.strftime('%d/%m/%Y')}", font=F_DATE, fill=CYAN)
        return rect, im

    def field_cpu_extra(self):
        """Linha abaixo do relogio: clock + temperatura do CPU + RAM usada/total."""
        clk = self.s.cpu_clock()
        tmp = self.s.cpu_temp()
        used, total, _ = self.s.ram()
        rect = (self.PX + 12, 70, 472, 90)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        x = 2
        if clk:
            s = f"{clk/1000:.2f} GHz"
            d.text((x, 1), s, font=F_SM, fill=WHITE)
            x += d.textlength(s, font=F_SM) + 10
        if tmp:
            s = f"{int(round(tmp))}°C"
            d.text((x, 1), s, font=F_SM, fill=CYAN)
            x += d.textlength(s, font=F_SM) + 10
        d.text((x, 1), f"{used:.0f}/{total:.0f} GB", font=F_SM, fill=GRAY)
        return rect, im

    def field_gpu(self, idx, g):
        y = 116 + idx * 38
        rect = (self.PX + 12, y + 16, 472, y + 34)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        d.text((2, 0), f"{g['vram_used']:.1f}/{g['vram_total']:.0f} GB", font=F_SM, fill=GRAY)
        d.text((120, 0), f"{int(g['temp'])}°C", font=F_SM, fill=CYAN)
        return rect, im

    def field_storage(self, disks):
        temps = [t for (_, t, _) in disks if t is not None]
        rect = (self.PX + 12, self._stor_y + 20, 472, self._stor_y + 58)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        rightx = rect[2] - rect[0]
        if temps:
            avg = sum(temps) / len(temps)
            name, hot, sz = max([d2 for d2 in disks if d2[1] is not None], key=lambda x: x[1])
            szs = f"{sz/1000:.0f}TB" if sz >= 1000 else f"{sz:.0f}GB"
            label = f"{name} {szs}"
            d.text((2, 0), f"Média:  {avg:.0f}°C", font=F_SM, fill=WHITE)
            d.text((2, 18), f"Máx:    {hot:.0f}°C", font=F_SM, fill=WHITE)
            lw = d.textlength(label, font=F_SM)
            d.text((rightx - lw, 18), label, font=F_SM, fill=CYAN)
        else:
            tot = sum(sz for (_, _, sz) in disks)
            d.text((2, 0), f"{len(disks)} discos", font=F_SM, fill=WHITE)
            d.text((2, 18), f"{tot/1000:.1f} TB total", font=F_SM, fill=GRAY)
        return rect, im

    def update(self, scr, sens, tick):
        cpu = sens.cpu_pct()
        _, _, rampct = sens.ram()
        gs = sens.gpus()
        gpu_avg = sum(g['use'] for g in gs) / len(gs) if gs else 0
        send_if_changed(scr, self, 'cpu', self.field_block_value(14, cpu))
        send_if_changed(scr, self, 'gpu', self.field_block_value(115, gpu_avg))
        send_if_changed(scr, self, 'ram', self.field_block_value(216, rampct))
        send_if_changed(scr, self, 'clk', self.field_clock())
        send_if_changed(scr, self, 'cpux', self.field_cpu_extra())
        for i, g in enumerate(gs[:2]):
            send_if_changed(scr, self, f'g{i}', self.field_gpu(i, g))
        if tick % 30 == 0:
            self._disks = sens.disks()
        if getattr(self, '_disks', None) is not None:
            send_if_changed(scr, self, 'stor', self.field_storage(self._disks))


class ThemePortrait(Theme):
    """Layout vertical 320x480 (BC-250): hora/data no topo; medidores CPU/RAM/GPU/SSD com a
    temperatura (grande) e um extra do mesmo tamanho ao lado - CPU/GPU mostram clock em MHz,
    RAM mostra a memoria disponivel; a direita o uso %, e uma barra abaixo. O SSD usa I/O
    momentaneo na barra principal + uma barra grande de 'Espaco usado'. Atualizacao dividida
    (%/barras a cada 1s; textos a cada ~2s; temp de disco a cada ~15s) para ficar fluido."""
    ROWS = (64, 148, 232, 316)
    LBLS = ('CPU', 'GPU', 'RAM', 'SSD')
    CTR = 15   # deslocamento ate o centro vertical do rotulo (alinhamento pelo meio)

    def __init__(self, sensors):
        self.s = sensors
        gs = self.s.gpus()
        self.gpu_names = [g['name'] for g in gs] or ['GPU']
        self.base = self._render_base()
        self._last = {}
        self._cache = {}

    def _bg(self):
        p = os.path.join(ASSETS, 'background_320x480.png')
        if os.path.exists(p):
            return Image.open(p).convert('RGB').resize((W, H), Image.LANCZOS)
        return Image.new('RGB', (W, H), (12, 8, 20))

    def _render_base(self):
        img = self._bg()
        d = ImageDraw.Draw(img, 'RGBA')
        d.line([(14, 54), (306, 54)], fill=(120, 90, 180, 160), width=1)
        for i, y in enumerate(self.ROWS):
            d.text((14, y + self.CTR), self.LBLS[i], font=F_CODE, fill=WHITE, anchor='lm')
        # rodape de marca (Orbitron)
        d.line([(14, 440), (306, 440)], fill=(120, 90, 180, 160), width=1)
        b1, b2 = "BC-250 ", "By Uller"
        w1 = d.textlength(b1, font=F_BRAND); w2 = d.textlength(b2, font=F_BRAND)
        x0 = (W - (w1 + w2)) / 2
        d.text((x0, 450), b1, font=F_BRAND, fill=(120, 210, 255))
        d.text((x0 + w1, 450), b2, font=F_BRAND, fill=(185, 145, 245))
        return img

    def field_time(self):
        rect = (12, 6, 178, 50)
        im = self._crop(rect)
        ImageDraw.Draw(im, 'RGBA').text((4, 4), br_now().strftime('%H:%M:%S'), font=F_CLK, fill=WHITE)
        return rect, im

    def field_date(self):
        now = br_now()
        rect = (172, 4, 316, 50)
        im = self._crop(rect)
        ImageDraw.Draw(im, 'RGBA').text((2, 13), f"{DIAS[now.weekday()]} {now.strftime('%d/%m/%Y')}",
                                        font=F_HDATE, fill=CYAN)
        return rect, im

    def field_txt(self, idx, temp, extra):
        """Esquerda do medidor: temperatura grande + valor extra (MHz/GB) do mesmo tamanho,
        centralizados verticalmente pelo centro do rotulo (CPU/GPU/RAM/SSD)."""
        y = self.ROWS[idx]
        rect = (72, y - 2, 242, y + 34)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        cy = self.CTR + 2   # centro vertical relativo ao crop (rect comeca em y-2)
        x = 6
        if temp is not None:
            t = f"{int(round(temp))}°C"
            d.text((x, cy), t, font=F_TEMP, fill=CYAN, anchor='lm')
            x += d.textlength(t, font=F_TEMP) + 8
        if extra:
            d.text((x, cy), extra, font=F_TEMP, fill=(212, 202, 232), anchor='lm')
        return rect, im

    def field_pct(self, idx, pct):
        y = self.ROWS[idx]
        rect = (242, y - 2, 308, y + 34)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        s = f"{int(round(pct))}%"
        d.text((rect[2] - rect[0] - 2, self.CTR + 2), s, font=F_TEMP, fill=WHITE, anchor='rm')
        return rect, im

    def field_bar(self, idx, pct):
        y = self.ROWS[idx]
        rect = (12, y + 38, 308, y + 58)
        im = self._crop(rect)
        self._progress(im, 2, 2, rect[2] - rect[0] - 4, 16, pct)
        return rect, im

    @staticmethod
    def _fmt_gb(g):
        if g >= 1000:
            s = f"{g/1000:.1f}".replace('.', ',')
            if s.endswith(',0'):
                s = s[:-2]
            return f"{s} TB"
        return f"{g:.0f} GB"

    def field_ssd_bars(self, busy_pct, space):
        space_pct, free_gb, total_gb = space
        y = self.ROWS[3]
        rect = (12, y + 38, 308, y + 122)
        im = self._crop(rect)
        d = ImageDraw.Draw(im, 'RGBA')
        bw = rect[2] - rect[0] - 4
        self._progress(im, 2, 2, bw, 16, busy_pct)             # uso (I/O) momentaneo do SSD
        # linha do espaco: "Espaco usado NN%" (esq) + "NNN GB livres de N TB" (dir), fonte maior
        left = f"Espaço usado {int(round(space_pct))}%"
        right = f"{self._fmt_gb(free_gb)} livres de {self._fmt_gb(total_gb)}"
        d.text((2, 30), left, font=F_SSDI, fill=(210, 200, 230))
        rw = d.textlength(right, font=F_SSDI)
        d.text((bw + 2 - rw, 30), right, font=F_SSDI, fill=CYAN)
        self._progress(im, 2, 56, bw, 24, space_pct)           # espaco usado (barra maior, mais respiro)
        return rect, im

    def _refresh_slow(self, sens):
        self._cache['cpu_t'] = sens.cpu_temp()
        self._cache['mem_t'] = sens.mem_temp()
        self._cache['ssd_t'] = sens.ssd_temp()

    def update(self, scr, sens, tick):
        if tick % 15 == 0 or not self._cache:
            self._refresh_slow(sens)
        c = self._cache
        cpu = sens.cpu_pct()
        avail, total = sens.mem_avail()
        used = max(0.0, total - avail)
        rp = (used / total * 100) if total else 0   # % e GB da RAM vem da mesma fonte
        gs = sens.gpus()
        gavg = sum(g['use'] for g in gs) / len(gs) if gs else 0
        gtemp = gs[0].get('temp') if gs else None
        sbusy = sens.ssd_busy()
        # rapido (cada segundo): hora, % e barras
        send_if_changed(scr, self, 'time', self.field_time())
        send_if_changed(scr, self, 'cpp', self.field_pct(0, cpu))
        send_if_changed(scr, self, 'cpb', self.field_bar(0, cpu))
        send_if_changed(scr, self, 'gpp', self.field_pct(1, gavg))
        send_if_changed(scr, self, 'gpb', self.field_bar(1, gavg))
        send_if_changed(scr, self, 'rmp', self.field_pct(2, rp))
        send_if_changed(scr, self, 'rmb', self.field_bar(2, rp))
        send_if_changed(scr, self, 'ssp', self.field_pct(3, sbusy))
        send_if_changed(scr, self, 'ssb', self.field_ssd_bars(sbusy, sens.ssd_space_info()))
        # mais lento (a cada 2s): temperatura + clock/mem (texto a esquerda)
        if tick % 2 == 0:
            cclk = sens.cpu_clock(); gclk = sens.gpu_clock()
            send_if_changed(scr, self, 'cpt', self.field_txt(0, c.get('cpu_t'), f"{cclk:.0f} MHz" if cclk else ""))
            send_if_changed(scr, self, 'gpt', self.field_txt(1, gtemp, f"{gclk:.0f} MHz" if gclk else ""))
            send_if_changed(scr, self, 'rmt', self.field_txt(2, c.get('mem_t'), f"{used:.0f}/{nice_mem(total)} GB"))
            send_if_changed(scr, self, 'sst', self.field_txt(3, c.get('ssd_t'), ""))
        if tick % 30 == 0:
            send_if_changed(scr, self, 'date', self.field_date())


# ---------------------------------------------------------------- LOOP
def send_if_changed(scr, theme, key, rect_im):
    rect, im = rect_im
    b = im.tobytes()
    if theme._last.get(key) == b:
        return
    theme._last[key] = b
    scr.blit(im, rect[0], rect[1])

def run(scr, sens, theme, interval=1.0):
    scr.init()
    scr.blit(theme.base, 0, 0)   # fundo + estaticos, 1x
    theme._last.clear()          # forca reenvio dos dinamicos apos (re)conectar
    print("conectado; fundo enviado, atualizando...")
    tick = 0
    while True:
        t0 = time.time()
        theme.update(scr, sens, tick)
        tick += 1
        dt = time.time() - t0
        time.sleep(max(0, interval - dt))

def test_pattern(scr):
    img = Image.new('RGB', (W, H), (255, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, H // 2, W, H], fill=(0, 0, 255))
    d.text((20, 20), "VERMELHO em cima / AZUL embaixo", fill=(255, 255, 255))
    scr.init(); scr.blit(img, 0, 0)
    print("Se em cima estiver VERMELHO e embaixo AZUL, a ordem RGB565 esta correta.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port')
    ap.add_argument('--be', action='store_true', help='RGB565 big-endian (padrao: little-endian)')
    ap.add_argument('--bg', action='store_true')
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--demo', action='store_true', help='dados ficticios do box ZimaOS')
    ap.add_argument('--portrait', action='store_true', help='layout vertical 320x480 (BC-250)')
    a = ap.parse_args()
    if a.portrait:
        global USE_PORTRAIT, W, H
        USE_PORTRAIT = True
        W, H = 320, 480
    print(f"RGB565: {'BE' if a.be else 'LE'}  orient={'portrait' if a.portrait else 'landscape'}  (Ctrl+C)")
    sens = DemoSensors() if a.demo else Sensors()
    ThemeCls = ThemePortrait if a.portrait else Theme

    # modos one-shot (--test / --bg)
    if a.test or a.bg:
        port = a.port or wait_for_port()
        try:
            scr = Screen(port, big_endian=a.be)
        except (serial.SerialException, OSError) as e:
            sys.exit(f"Nao abriu {port}: {e}\n>> No Windows feche o UsbMonitor.exe (segura a porta).")
        try:
            if a.test:
                test_pattern(scr)
            else:
                theme = ThemeCls(sens); scr.init(); scr.blit(theme.base, 0, 0); print("fundo enviado.")
        finally:
            scr.close()
        return

    # modo servico: acha a tela em qualquer USB, roda e reconecta sozinho
    while True:
        port = a.port or wait_for_port()
        scr = None
        try:
            scr = Screen(port, big_endian=a.be)
            print(f"tela encontrada em {port}")
            theme = ThemeCls(sens)            # CPU/GPU/RAM frescos a cada conexao
            run(scr, sens, theme)
        except KeyboardInterrupt:
            print("\nsaindo.")
            return
        except (serial.SerialException, OSError) as e:
            print(f"tela desconectada ou erro: {e}  ->  reprocurando em 2s")
            time.sleep(2)
        finally:
            if scr:
                try: scr.close()
                except Exception: pass

if __name__ == '__main__':
    main()
