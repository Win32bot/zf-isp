"""ZF-ISP — прошивальщик AVR через ZhiFeng USBISP (HID 03EB:C8B4) в обход PROGISP.

Протокол программатора (HID feature report ID 1, 8 байт):
  [1,0x0D,1,spd,0x20,0xA0,0x40,0xC0] + [1,0x0D,0,spd,...]  — вход в ISP (spd 1=быстро .. 5=медленно)
  [1,0x0E,a,b,c,d,0,4]                                      — сырая 4-байтная ISP-команда
  GetFeature(1) -> байты 0..3 = ответ SPI. Команда выполняется ТОЛЬКО при GetFeature.
  [1,0x0B,1,0,...]                                          — выход из ISP

ISP-команды каждого чипа берутся из chips_db.py (генерируется gen_chips.py из avrdude.conf AVRDUDESS).
"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import hid

from chips_db import CHIPS

VID, PID = 0x03EB, 0xC8B4
APP = 'ZF-ISP'
FUSE_KEYS = ('low', 'high', 'ext', 'lock')
FUSE_TITLE = {'low': 'Low', 'high': 'High', 'ext': 'Extended', 'lock': 'Lock'}


class Abort(Exception):
    pass


# ---------------------------------------------------------------- формат SPI-команд avrdude
_pat_cache = {}


def pattern(p):
    """'0010.0000--aaaa.aaaa--...' или '0 0 1 0 ... a7 a6 ...' -> 32 токена (MSB байта 0 первым)"""
    t = _pat_cache.get(p)
    if t is None:
        if ' ' in p.strip():
            t = p.split()
        else:
            t = list(p.replace('--', '').replace('.', ''))
        if len(t) != 32:
            raise ValueError('плохой формат команды: ' + p)
        _pat_cache[p] = t
    return t


def encode(p, addr=0, data=0):
    out = [0, 0, 0, 0]
    for idx, tok in enumerate(pattern(p)):
        byte, pos = divmod(idx, 8); bit = 7 - pos; v = 0
        if tok == '1':
            v = 1
        elif tok[0] == 'a':
            n = int(tok[1:]) if len(tok) > 1 else ((2 - byte) * 8 + bit if byte in (1, 2) else bit)
            v = (addr >> n) & 1
        elif tok[0] == 'i':
            n = int(tok[1:]) if len(tok) > 1 else bit
            v = (data >> n) & 1
        if v:
            out[byte] |= 1 << bit
    return out


def decode(p, resp):
    val = 0
    for idx, tok in enumerate(pattern(p)):
        if tok[0] == 'o':
            byte, pos = divmod(idx, 8); bit = 7 - pos
            n = int(tok[1:]) if len(tok) > 1 else bit
            if (resp[byte] >> bit) & 1:
                val |= 1 << n
    return val


# ---------------------------------------------------------------- программатор
class Programmer:
    def __init__(self, speed=1):
        self.speed = speed
        self.d = hid.device()
        try:
            self.d.open(VID, PID)
        except OSError:
            raise RuntimeError('Программатор 03EB:C8B4 не найден (или занят PROGISP)')

    def _set(self, b):
        if self.d.send_feature_report(bytes(b)) <= 0:
            raise RuntimeError('Ошибка записи в программатор')

    def raw(self, b4):
        self._set([1, 0x0E] + list(b4) + [0, 4])
        return list(self.d.get_feature_report(1, 8)[:4])

    def run(self, p, addr=0, data=0):
        return decode(p, self.raw(encode(p, addr, data)))

    def enter(self):
        for _ in range(3):
            self._set([1, 0x0D, 1, self.speed, 0x20, 0xA0, 0x40, 0xC0])
            self._set([1, 0x0D, 0, self.speed, 0x20, 0xA0, 0x40, 0xC0])
            if self.raw([0xAC, 0x53, 0, 0])[2] == 0x53:
                return True
            self.leave(); time.sleep(0.05)
        return False   # старые чипы (AT90S1200) не отвечают эхом — решаем по сигнатуре

    def leave(self):
        try:
            self._set([1, 0x0B, 1, 0, 0, 0, 0, 0])
        except Exception:
            pass

    def close(self):
        self.leave()
        try:
            self.d.close()
        except Exception:
            pass

    def signature(self):
        return tuple(self.raw([0x30, 0, i, 0])[3] for i in range(3))

    # --- операции по описанию чипа
    def read_fuse(self, c, k):
        return self.run(c['fuses'][k]['read'])

    def write_fuse(self, c, k, v):
        f = c['fuses'][k]
        self.run(f['write'], data=v)
        time.sleep(f['delay'] * 1.2e-6 + 0.002)

    def chip_erase(self, c):
        self.run(c['erase'])
        time.sleep(c['erase_delay'] * 1.5e-6 + 0.02)

    def write_flash(self, c, img, progress, stop):
        fl = c['flash']; size = fl['size']
        if fl['load_lo'] and fl['writepage']:
            page = fl['page']
            pages = [pa for pa in range(0, size, page) if img[pa:pa + page] != b'\xff' * page]
            ext = -1
            for n, pa in enumerate(pages):
                if stop.is_set():
                    raise Abort()
                for a in range(pa, pa + page, 2):
                    w = a >> 1
                    # грузим все байты, включая FF: буфер страницы в ISP не очищается
                    self.run(fl['load_lo'], w, img[a])
                    self.run(fl['load_hi'], w, img[a + 1])
                w = pa >> 1
                if fl['ext'] and (w >> 16) != ext:
                    ext = w >> 16; self.run(fl['ext'], ext)
                self.run(fl['writepage'], w)
                time.sleep(fl['delay'] * 1.1e-6 + 0.0005)
                progress(n + 1, len(pages))
        else:   # старые чипы без страниц: побайтная запись
            words = [a for a in range(0, size, 2) if img[a] != 0xFF or img[a + 1] != 0xFF]
            for n, a in enumerate(words):
                if stop.is_set():
                    raise Abort()
                for p, b in ((fl['write_lo'], img[a]), (fl['write_hi'], img[a + 1])):
                    if b != 0xFF:
                        self.run(p, a >> 1, b)
                        time.sleep(fl['delay'] * 1.1e-6 + 0.0005)
                if n % 64 == 0:
                    progress(n + 1, len(words))
            progress(1, 1)

    def read_flash(self, c, progress, stop, until=None):
        fl = c['flash']; end = until or fl['size']
        out = bytearray(); ext = -1
        for a in range(0, end, 2):
            if stop.is_set():
                raise Abort()
            w = a >> 1
            if fl['ext'] and (w >> 16) != ext:
                ext = w >> 16; self.run(fl['ext'], ext)
            out.append(self.run(fl['read_lo'], w))
            out.append(self.run(fl['read_hi'], w))
            if a % 1024 == 0:
                progress(a, end)
        progress(end, end)
        return out

    def read_eeprom(self, c, progress, stop, until=None):
        ee = c['eeprom']; end = until or ee['size']
        out = bytearray()
        for a in range(end):
            if stop.is_set():
                raise Abort()
            out.append(self.run(ee['read'], a))
            if a % 256 == 0:
                progress(a, end)
        progress(end, end)
        return out

    def write_eeprom(self, c, data, progress, stop):
        ee = c['eeprom']; size = len(data)
        dly = ee['delay'] * 1.2e-6 + 0.001
        if ee['page'] > 1 and ee['load'] and ee['writepage']:
            page = ee['page']
            for pa in range(0, size, page):
                if stop.is_set():
                    raise Abort()
                for a in range(pa, min(pa + page, size)):
                    self.run(ee['load'], a, data[a])
                self.run(ee['writepage'], pa)
                time.sleep(dly)
                progress(pa + page, size)
        else:
            for a in range(size):
                if stop.is_set():
                    raise Abort()
                self.run(ee['write'], a, data[a])
                time.sleep(dly)
                if a % 16 == 0:
                    progress(a + 1, size)
        progress(size, size)


# ---------------------------------------------------------------- Intel HEX
def load_hex(path, size):
    img = bytearray(b'\xff' * size); base = 0; end = 0
    with open(path) as f:
        for no, ln in enumerate(f, 1):
            ln = ln.strip()
            if not ln:
                continue
            if not ln.startswith(':'):
                raise ValueError('строка %d: не Intel HEX' % no)
            b = bytes.fromhex(ln[1:])
            if sum(b) & 0xFF:
                raise ValueError('строка %d: неверная контрольная сумма' % no)
            n, a, t = b[0], (b[1] << 8) | b[2], b[3]
            if t == 0:
                addr = base + a
                if addr + n > size:
                    raise ValueError('данные по адресу 0x%X не помещаются в память чипа (%d байт)' % (addr, size))
                img[addr:addr + n] = b[4:4 + n]; end = max(end, addr + n)
            elif t == 1:
                break
            elif t == 2:
                base = ((b[4] << 8) | b[5]) << 4
            elif t == 4:
                base = ((b[4] << 8) | b[5]) << 16
    return img, end


def save_hex(path, data, trim=True):
    last = len(data)
    if trim:
        while last > 0 and data[last - 1] == 0xFF:
            last -= 1
    lines = []; seg = -1
    for a in range(0, last, 16):
        if a >> 16 != seg:
            seg = a >> 16
            rec = bytes([2, 0, 0, 4, seg >> 8, seg & 0xFF])
            lines.append(':' + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
        chunk = data[a:min(a + 16, last)]
        rec = bytes([len(chunk), (a >> 8) & 0xFF, a & 0xFF, 0]) + bytes(chunk)
        lines.append(':' + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    lines.append(':00000001FF')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def bit_names(c, k):
    """имена битов 7..0 для фьюза k ('' = нет имени)"""
    if c and c.get('bits') and k in c['bits']:
        return c['bits'][k]
    return [''] * 8


def find_bit(c, name):
    """-> (фьюз, номер бита) или None"""
    for k in FUSE_KEYS:
        if k in c['fuses']:
            names = bit_names(c, k)
            if name in names:
                return k, 7 - names.index(name)
    return None


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title('%s — %d чипов AVR' % (APP, len(CHIPS)))
        root.minsize(800, 720)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.chip = None
        self.fuse_vars, self.fuse_entries, self.fuse_titles, self.bit_vars = {}, {}, {}, {}
        self._updating = False
        self.buttons = []
        pad = dict(padx=6, pady=3)

        # --- подключение
        top = ttk.LabelFrame(root, text='Программатор и чип')
        top.pack(fill='x', **pad)
        ttk.Label(top, text='Скорость ISP:').grid(row=0, column=0, sticky='w', **pad)
        self.speed = ttk.Combobox(top, width=18, state='readonly',
                                  values=['1 — High (быстро)', '2', '3', '4', '5 — Low (медленно)'])
        self.speed.current(0)
        self.speed.grid(row=0, column=1, sticky='w', **pad)
        b = ttk.Button(top, text='Подключить / прочитать', command=self.do_connect)
        b.grid(row=0, column=2, **pad); self.buttons.append(b)
        self.chip_lbl = ttk.Label(top, text='Чип: —', font=('Segoe UI', 10, 'bold'))
        self.chip_lbl.grid(row=0, column=3, sticky='w', **pad)

        # --- фьюзы
        ff = ttk.LabelFrame(root, text='Фьюзы и Lock  (галочка = бит запрограммирован, т.е. 0)')
        ff.pack(fill='x', **pad)
        for col, k in enumerate(FUSE_KEYS):
            fr = ttk.Frame(ff)
            fr.grid(row=0, column=col, sticky='n', padx=8, pady=2)
            lbl = ttk.Label(fr, text=FUSE_TITLE[k], font=('Segoe UI', 9, 'bold'))
            lbl.pack(anchor='w'); self.fuse_titles[k] = lbl
            v = tk.StringVar(value='')
            v.trace_add('write', lambda *_, k=k: self.hex_changed(k))
            e = ttk.Entry(fr, textvariable=v, width=6, justify='center', font=('Consolas', 11))
            e.pack(anchor='w', pady=2)
            self.fuse_vars[k] = v; self.fuse_entries[k] = e
            self.bit_vars[k] = []
            for bit in range(8):
                bv = tk.IntVar(value=0)
                cb = ttk.Checkbutton(fr, text='бит %d' % (7 - bit), variable=bv, command=lambda k=k: self.bits_changed(k))
                cb.pack(anchor='w')
                self.bit_vars[k].append((bv, cb))
        bf = ttk.Frame(ff)
        bf.grid(row=0, column=4, sticky='n', padx=8, pady=2)
        for txt, cmd in [('Прочитать', self.do_connect), ('Записать фьюзы', self.do_write_fuses),
                         ('Записать Lock', self.do_write_lock)]:
            b = ttk.Button(bf, text=txt, command=cmd, width=18)
            b.pack(pady=3); self.buttons.append(b)
        self.fuse_hint = ttk.Label(bf, text='', foreground='#a00', wraplength=190, justify='left')
        self.fuse_hint.pack(pady=4)

        # --- flash
        fl = ttk.LabelFrame(root, text='Flash')
        fl.pack(fill='x', **pad)
        self.hexpath = tk.StringVar()
        ttk.Label(fl, text='HEX:').grid(row=0, column=0, **pad)
        ttk.Entry(fl, textvariable=self.hexpath).grid(row=0, column=1, sticky='we', **pad)
        b = ttk.Button(fl, text='…', width=3, command=lambda: self.browse(self.hexpath, 'Intel HEX', '*.hex'))
        b.grid(row=0, column=2, **pad); self.buttons.append(b)
        fl.columnconfigure(1, weight=1)
        opt = ttk.Frame(fl); opt.grid(row=1, column=0, columnspan=3, sticky='w')
        self.o_erase = tk.IntVar(value=1); self.o_write = tk.IntVar(value=1); self.o_verify = tk.IntVar(value=1)
        for t, v in [('Стереть чип', self.o_erase), ('Записать', self.o_write), ('Проверить', self.o_verify)]:
            ttk.Checkbutton(opt, text=t, variable=v).pack(side='left', padx=6)
        act = ttk.Frame(fl); act.grid(row=2, column=0, columnspan=3, sticky='w', pady=3)
        for txt, cmd in [('Прошить', self.do_flash), ('Только проверить', self.do_verify_only),
                         ('Прочитать в файл', self.do_read_flash), ('Стереть чип', self.do_erase)]:
            b = ttk.Button(act, text=txt, command=cmd); b.pack(side='left', padx=6); self.buttons.append(b)

        # --- eeprom
        ef = ttk.LabelFrame(root, text='EEPROM')
        ef.pack(fill='x', **pad)
        self.eeppath = tk.StringVar()
        ttk.Label(ef, text='Файл:').grid(row=0, column=0, **pad)
        ttk.Entry(ef, textvariable=self.eeppath).grid(row=0, column=1, sticky='we', **pad)
        b = ttk.Button(ef, text='…', width=3, command=lambda: self.browse(self.eeppath, 'EEPROM (Intel HEX)', '*.eep *.hex'))
        b.grid(row=0, column=2, **pad); self.buttons.append(b)
        ef.columnconfigure(1, weight=1)
        act = ttk.Frame(ef); act.grid(row=1, column=0, columnspan=3, sticky='w', pady=3)
        for txt, cmd in [('Записать EEPROM', self.do_write_eeprom), ('Проверить EEPROM', self.do_verify_eeprom),
                         ('Прочитать EEPROM в файл', self.do_read_eeprom)]:
            b = ttk.Button(act, text=txt, command=cmd); b.pack(side='left', padx=6); self.buttons.append(b)
        self.ee_hint = ttk.Label(act, text='', foreground='#555'); self.ee_hint.pack(side='left', padx=10)

        # --- прогресс, журнал, статус
        pf = ttk.Frame(root); pf.pack(fill='x', **pad)
        self.pbar = ttk.Progressbar(pf, mode='determinate', maximum=100)
        self.pbar.pack(fill='x', side='left', expand=True)
        self.pct = ttk.Label(pf, text='', width=24); self.pct.pack(side='left', padx=6)
        self.stop_btn = ttk.Button(pf, text='Стоп', command=self.stop.set, state='disabled')
        self.stop_btn.pack(side='left')
        lf = ttk.Frame(root); lf.pack(fill='both', expand=True, **pad)
        self.log = tk.Text(lf, height=7, font=('Consolas', 9), state='disabled')
        sb = ttk.Scrollbar(lf, command=self.log.yview); self.log['yscrollcommand'] = sb.set
        self.log.pack(side='left', fill='both', expand=True); sb.pack(side='right', fill='y')
        self.status = ttk.Label(root, text='Готов', relief='sunken', anchor='w')
        self.status.pack(fill='x', side='bottom')

        self.apply_chip(None)
        root.after(50, self.poll)

    # ---------- отображение чипа
    def apply_chip(self, c):
        self.chip = c
        for k in FUSE_KEYS:
            has = bool(c and k in c['fuses'])
            title = FUSE_TITLE[k]
            if c and k == 'low' and has and c['fuses'][k]['mem'] == 'fuse':
                title = 'Fuse'
            self.fuse_titles[k].configure(text=title)
            self.fuse_entries[k].configure(state='normal' if (has or not c) else 'disabled')
            if c and not has:
                self.fuse_vars[k].set('')
            names = bit_names(c, k)
            mask = c['fuses'][k]['mask'] if has else 0xFF
            for i, (bv, cb) in enumerate(self.bit_vars[k]):
                bit = 7 - i
                used = has and (mask >> bit) & 1 if c else True
                cb.configure(text=names[i] or ('бит %d' % bit if used else '–'),
                             state='normal' if used else 'disabled')
        ee = c and c['eeprom']
        self.ee_hint.configure(text=('%d байт' % ee['size']) if ee else ('нет EEPROM' if c else ''))
        self.check_fuses()

    # ---------- синхронизация hex <-> галочки
    def hex_changed(self, k):
        if self._updating:
            return
        v = self.parse(self.fuse_vars[k].get())
        if v is None:
            return
        self._updating = True
        for i, (bv, cb) in enumerate(self.bit_vars[k]):
            bv.set(0 if (v >> (7 - i)) & 1 else 1)
        self._updating = False
        self.check_fuses()

    def bits_changed(self, k):
        cur = self.parse(self.fuse_vars[k].get())
        v = 0
        for i, (bv, cb) in enumerate(self.bit_vars[k]):
            bit = 7 - i
            if str(cb.cget('state')) == 'disabled' and cur is not None:
                v |= cur & (1 << bit)        # неиспользуемые биты не трогаем
            elif not bv.get():
                v |= 1 << bit
        self._updating = True
        self.fuse_vars[k].set('%02X' % v)
        self._updating = False
        self.check_fuses()

    @staticmethod
    def parse(s):
        s = s.strip().upper().replace('0X', '')
        try:
            v = int(s, 16)
            return v if 0 <= v <= 0xFF else None
        except ValueError:
            return None

    def danger(self, vals):
        """-> (блокирующие ошибки, предупреждения) для значений фьюзов"""
        errs, warns = [], []
        c = self.chip
        if not c:
            return errs, warns
        for name, bad, msg, block in (
                ('SPIEN', 1, 'SPIEN не запрограммирован — ISP перестанет работать', True),
                ('RSTDISBL', 0, 'RSTDISBL запрограммирован — ножка RESET отключится, ISP перестанет работать', True),
                ('DWEN', 0, 'DWEN запрограммирован — включится debugWIRE, ISP будет недоступен до его выключения', False),
                ('JTAGEN', 0, 'JTAGEN включён — JTAG займёт свои ножки порта', False)):
            pos = find_bit(c, name)
            if not pos:
                continue
            v = vals.get(pos[0])
            if v is None:
                continue
            if (v >> pos[1]) & 1 == bad:
                (errs if block else warns).append(msg)
        return errs, warns

    def check_fuses(self):
        vals = {k: self.parse(self.fuse_vars[k].get()) for k in ('low', 'high', 'ext')}
        errs, warns = self.danger(vals)
        self.fuse_hint.configure(text='\n'.join(errs + warns))

    # ---------- UI helpers
    def logmsg(self, s):
        self.log.configure(state='normal')
        self.log.insert('end', time.strftime('%H:%M:%S  ') + s + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def browse(self, var, title, pat):
        p = filedialog.askopenfilename(filetypes=[(title, pat), ('Все файлы', '*.*')])
        if p:
            var.set(p)

    def poll(self):
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == 'log':
                    self.logmsg(a[0])
                elif kind == 'status':
                    self.status.configure(text=a[0])
                elif kind == 'progress':
                    done, total, text = a
                    self.pbar['value'] = 100.0 * done / total if total else 0
                    self.pct.configure(text=text)
                elif kind == 'chip':
                    if a[0] is not self.chip:
                        self.apply_chip(a[0])
                    self.chip_lbl.configure(text='Чип: ' + a[1])
                elif kind == 'fuses':
                    for k, v in a[0].items():
                        self.fuse_vars[k].set('' if v is None else '%02X' % v)
                elif kind == 'done':
                    self.set_busy(False)
                elif kind == 'error':
                    messagebox.showerror(APP, a[0])
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def set_busy(self, busy):
        for b in self.buttons:
            b.configure(state='disabled' if busy else 'normal')
        self.speed.configure(state='disabled' if busy else 'readonly')
        self.stop_btn.configure(state='normal' if busy else 'disabled')

    # ---------- фоновые операции
    def run(self, title, fn):
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self.set_busy(True)
        self.q.put(('status', title + '…'))
        self.q.put(('progress', 0, 1, ''))
        speed = self.speed.current() + 1

        def body():
            p = None
            t0 = time.time()
            try:
                p = Programmer(speed)
                synced = p.enter()
                sig = p.signature()
                sigs = ' '.join('%02X' % x for x in sig)
                c = CHIPS.get(sig)
                if c is None or (not synced and c['pollindex']):
                    if not synced or sig in ((0xFF, 0xFF, 0xFF), (0, 0, 0)):
                        raise RuntimeError('Чип не отвечает (сигнатура %s). Проверьте подключение и питание, '
                                           'для нового чипа поставьте скорость 5 (Low)' % sigs)
                    raise RuntimeError('Неизвестный чип, сигнатура %s' % sigs)
                ee = c['eeprom']
                self.q.put(('chip', c, '%s  (%s, flash %d КБ%s)' % (
                    c['name'], sigs, c['flash']['size'] // 1024, ', EEPROM %d Б' % ee['size'] if ee else '')))
                msg = fn(p, c)
                self.q.put(('status', '%s — готово (%.0f с)' % (msg or title, time.time() - t0)))
            except Abort:
                self.q.put(('log', 'Остановлено пользователем'))
                self.q.put(('status', title + ' — остановлено'))
            except Exception as e:
                self.q.put(('log', 'ОШИБКА: %s' % e))
                self.q.put(('status', title + ' — ошибка'))
                self.q.put(('error', str(e)))
            finally:
                if p:
                    p.close()
                self.q.put(('done',))

        self.worker = threading.Thread(target=body, daemon=True)
        self.worker.start()

    def progress_cb(self, verb):
        t0 = time.time()

        def cb(done, total):
            el = time.time() - t0
            eta = el / done * (total - done) if done else 0
            self.q.put(('progress', done, total, '%s %d%%  ~%d с' % (verb, 100 * done // total if total else 0, eta)))
        return cb

    def read_all_fuses(self, p, c):
        return {k: (p.read_fuse(c, k) if k in c['fuses'] else None) for k in FUSE_KEYS}

    @staticmethod
    def fmt_fuses(f):
        return '  '.join('%s=%s' % (FUSE_TITLE[k], '--' if f[k] is None else '%02X' % f[k]) for k in FUSE_KEYS)

    def need(self, c, what):
        if what == 'eeprom' and not c['eeprom']:
            raise RuntimeError('У %s нет EEPROM' % c['name'])

    # ---------- действия
    def do_connect(self):
        def fn(p, c):
            f = self.read_all_fuses(p, c)
            self.q.put(('fuses', f))
            self.q.put(('log', '%s: %s' % (c['name'], self.fmt_fuses(f))))
            return 'Подключено'
        self.run('Подключение', fn)

    def do_write_fuses(self):
        c = self.chip
        if not c:
            messagebox.showwarning(APP, 'Сначала нажмите «Подключить / прочитать»')
            return
        vals = {k: self.parse(self.fuse_vars[k].get()) for k in ('low', 'high', 'ext') if k in c['fuses']}
        miss = [FUSE_TITLE[k] for k, v in vals.items() if v is None]
        if miss:
            messagebox.showwarning(APP, 'Не заполнены: ' + ', '.join(miss))
            return
        errs, warns = self.danger(vals)
        if errs:
            messagebox.showerror(APP, 'Запись отменена:\n' + '\n'.join(errs))
            return
        txt = ', '.join('%s=%02X' % (FUSE_TITLE[k], v) for k, v in vals.items())
        if not messagebox.askyesno(APP, 'Записать в %s: %s?%s' % (c['name'], txt, ('\n\n' + '\n'.join(warns)) if warns else '')):
            return
        chip = c

        def fn(p, c):
            if c is not chip:
                raise RuntimeError('Подключён другой чип (%s). Прочитайте фьюзы заново' % c['name'])
            for k, v in vals.items():
                p.write_fuse(c, k, v)
            f = self.read_all_fuses(p, c)
            self.q.put(('fuses', f))
            self.q.put(('log', 'Записано, прочитано: ' + self.fmt_fuses(f)))
            bad = [FUSE_TITLE[k] for k, v in vals.items() if (f[k] ^ v) & c['fuses'][k]['mask']]
            if bad:
                raise RuntimeError('После записи не совпали: ' + ', '.join(bad))
            return 'Фьюзы записаны'
        self.run('Запись фьюзов', fn)

    def do_write_lock(self):
        c = self.chip
        if not c or 'lock' not in c['fuses']:
            messagebox.showwarning(APP, 'Сначала нажмите «Подключить / прочитать»' if not c else 'Lock не поддерживается')
            return
        v = self.parse(self.fuse_vars['lock'].get())
        if v is None:
            messagebox.showwarning(APP, 'Введите значение Lock в hex')
            return
        lb = find_bit(c, 'LB1')
        locked = (lb and not (v >> lb[1]) & 1) or (not lb and (v & 3) != 3)
        extra = ('\n\nЗащита включается: чтение и проверка flash/EEPROM станут невозможны, '
                 'снять можно только стиранием чипа.') if locked else ''
        if not messagebox.askyesno(APP, 'Записать Lock=%02X?%s' % (v, extra)):
            return

        def fn(p, c):
            p.write_fuse(c, 'lock', v)
            f = self.read_all_fuses(p, c)
            self.q.put(('fuses', f))
            self.q.put(('log', 'Lock записан, прочитано: ' + self.fmt_fuses(f)))
            if (f['lock'] ^ v) & c['fuses']['lock']['mask']:
                raise RuntimeError('Lock после записи = %02X, ожидалось %02X' % (f['lock'], v))
            return 'Lock записан'
        self.run('Запись Lock', fn)

    def do_erase(self):
        if not messagebox.askyesno(APP, 'Стереть чип? Сотрётся flash, Lock и EEPROM (если не стоит EESAVE).'):
            return

        def fn(p, c):
            p.chip_erase(c)
            self.q.put(('log', 'Чип стёрт'))
            self.q.put(('fuses', self.read_all_fuses(p, c)))
            return 'Чип стёрт'
        self.run('Стирание', fn)

    def _load(self, var, size, what):
        path = var.get().strip()
        if not path or not os.path.isfile(path):
            raise RuntimeError('Не выбран файл ' + what)
        img, end = load_hex(path, size)
        self.q.put(('log', '%s: %s, данные до 0x%X' % (what, os.path.basename(path), end)))
        return img, end

    def _compare(self, data, img, what, t0):
        bad = [a for a in range(len(data)) if data[a] != img[a]]
        if bad:
            ex = ', '.join('0x%05X: чип %02X, файл %02X' % (a, data[a], img[a]) for a in bad[:5])
            raise RuntimeError('%s: проверка не прошла, %d несовпадений. %s' % (what, len(bad), ex))
        self.q.put(('log', '%s: проверка OK (%d байт, %.0f с)' % (what, len(data), time.time() - t0)))

    def _verify_flash(self, p, c, img, end):
        self.q.put(('status', 'Проверка flash…')); t0 = time.time()
        data = p.read_flash(c, self.progress_cb('Проверка'), self.stop, until=(end + 1) & ~1)
        self._compare(data, img, 'Flash', t0)

    def do_flash(self):
        if not (self.o_erase.get() or self.o_write.get() or self.o_verify.get()):
            return
        if self.o_write.get() and not self.o_erase.get():
            if not messagebox.askyesno(APP, 'Запись без стирания обычно даёт ошибки проверки. Продолжить?'):
                return

        def fn(p, c):
            img, end = self._load(self.hexpath, c['flash']['size'], 'Flash')
            if self.o_erase.get():
                p.chip_erase(c); self.q.put(('log', 'Чип стёрт'))
            if self.o_write.get():
                self.q.put(('status', 'Запись flash…')); t0 = time.time()
                p.write_flash(c, img, self.progress_cb('Запись'), self.stop)
                self.q.put(('log', 'Flash записан (%.0f с)' % (time.time() - t0)))
            if self.o_verify.get():
                self._verify_flash(p, c, img, end)
            return 'Прошивка'
        self.run('Прошивка', fn)

    def do_verify_only(self):
        def fn(p, c):
            img, end = self._load(self.hexpath, c['flash']['size'], 'Flash')
            self._verify_flash(p, c, img, end)
            return 'Проверка OK'
        self.run('Проверка', fn)

    def do_read_flash(self):
        path = filedialog.asksaveasfilename(defaultextension='.hex', filetypes=[('Intel HEX', '*.hex')])
        if not path:
            return

        def fn(p, c):
            t0 = time.time()
            data = p.read_flash(c, self.progress_cb('Чтение'), self.stop)
            save_hex(path, data)
            self.q.put(('log', 'Flash прочитан в %s (%.0f с)' % (path, time.time() - t0)))
            return 'Чтение flash'
        self.run('Чтение flash', fn)

    # --- EEPROM
    def do_write_eeprom(self):
        def fn(p, c):
            self.need(c, 'eeprom')
            img, end = self._load(self.eeppath, c['eeprom']['size'], 'EEPROM')
            n = end if end else c['eeprom']['size']
            self.q.put(('status', 'Запись EEPROM…')); t0 = time.time()
            p.write_eeprom(c, img[:n], self.progress_cb('Запись EEPROM'), self.stop)
            self.q.put(('log', 'EEPROM записан (%d байт, %.0f с)' % (n, time.time() - t0)))
            self.q.put(('status', 'Проверка EEPROM…')); t0 = time.time()
            data = p.read_eeprom(c, self.progress_cb('Проверка EEPROM'), self.stop, until=n)
            self._compare(data, img, 'EEPROM', t0)
            return 'EEPROM записан'
        self.run('Запись EEPROM', fn)

    def do_verify_eeprom(self):
        def fn(p, c):
            self.need(c, 'eeprom')
            img, end = self._load(self.eeppath, c['eeprom']['size'], 'EEPROM')
            t0 = time.time()
            data = p.read_eeprom(c, self.progress_cb('Проверка EEPROM'), self.stop, until=end or None)
            self._compare(data, img, 'EEPROM', t0)
            return 'Проверка EEPROM OK'
        self.run('Проверка EEPROM', fn)

    def do_read_eeprom(self):
        path = filedialog.asksaveasfilename(defaultextension='.eep',
                                            filetypes=[('EEPROM (Intel HEX)', '*.eep'), ('Intel HEX', '*.hex')])
        if not path:
            return

        def fn(p, c):
            self.need(c, 'eeprom')
            t0 = time.time()
            data = p.read_eeprom(c, self.progress_cb('Чтение EEPROM'), self.stop)
            save_hex(path, data, trim=False)
            self.q.put(('log', 'EEPROM (%d байт) прочитан в %s (%.0f с)' % (len(data), path, time.time() - t0)))
            return 'Чтение EEPROM'
        self.run('Чтение EEPROM', fn)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except tk.TclError:
        pass
    # Ctrl+C/V/X/A в полях ввода при русской раскладке (Tk смотрит на символ, а не на клавишу)
    def ctrl_keys(e):
        ev = {86: '<<Paste>>', 67: '<<Copy>>', 88: '<<Cut>>'}.get(e.keycode)
        if len(e.keysym) == 1 and e.keysym.isascii():
            return None                      # латиница — штатные привязки Tk
        if ev:
            e.widget.event_generate(ev); return 'break'
        if e.keycode == 65 and isinstance(e.widget, (tk.Entry, ttk.Entry)):
            e.widget.select_range(0, 'end'); e.widget.icursor('end'); return 'break'
    root.bind_class('TEntry', '<Control-KeyPress>', ctrl_keys, add='+')
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
