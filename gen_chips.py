"""Генерирует chips_db.py из avrdude.conf + bits.xml (AVRDUDESS).

python gen_chips.py ["C:/Program Files (x86)/AVRDUDESS"]
Берутся только чипы с ISP (PM_ISP), известной сигнатурой и SPI-командами чтения/записи flash.
"""
import os
import pprint
import re
import sys
import xml.etree.ElementTree as ET

SRC = sys.argv[1] if len(sys.argv) > 1 else r'C:\Program Files (x86)\AVRDUDESS'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chips_db.py')

TOK = re.compile(r'"(?:[^"\\]|\\.)*"|[A-Za-z0-9_.+\-]+|[=;,|]')


def tokens(text):
    out = []
    for line in text.splitlines():
        # срезаем комментарий вне кавычек
        q = False
        for i, ch in enumerate(line):
            if ch == '"':
                q = not q
            elif ch == '#' and not q:
                line = line[:i]
                break
        out.extend(TOK.findall(line))
    return out


def parse(text):
    """-> list of dict(parent=..., fields={...}, mems={name: {...} | None})"""
    t = tokens(text); i = 0; parts = []

    def stmt_value(i):
        vals = []
        while t[i] != ';':
            if t[i] not in (',', '|'):
                vals.append(t[i].strip('"') if t[i].startswith('"') else t[i])
            i += 1
        return vals, i + 1

    while i < len(t):
        tok = t[i]
        if tok in ('part', 'programmer', 'serialadapter'):
            kind = tok; i += 1
            parent = None
            if t[i] == 'parent':
                parent = t[i + 1].strip('"'); i += 2
            fields, mems = {}, {}
            while t[i] != ';':
                if t[i] == 'memory':
                    name = t[i + 1].strip('"'); i += 2
                    if t[i] == '=':          # memory "x" = NULL;
                        mems[name] = None; i += 3; continue
                    mf = {}
                    while t[i] != ';':
                        key = t[i]
                        if t[i + 1] == '=':
                            v, i = stmt_value(i + 2); mf[key] = v
                        else:                # alias "prodsig"; и т.п.
                            v, i = stmt_value(i + 1); mf[key] = v
                    i += 1
                    mems[name] = mf
                else:
                    key = t[i]
                    if t[i + 1] == '=':
                        v, i = stmt_value(i + 2)
                    else:
                        v, i = stmt_value(i + 1)
                    fields[key] = v
            i += 1
            if kind == 'part':
                parts.append(dict(parent=parent, fields=fields, mems=mems))
        else:
            # глобальное присваивание: key = value ;
            while t[i] != ';':
                i += 1
            i += 1
    return parts


def resolve(parts):
    by_id = {}
    for p in parts:
        pid = p['fields'].get('id', [None])[0]
        if pid:
            by_id[pid] = p
    cache = {}

    def get(pid):
        if pid in cache:
            return cache[pid]
        p = by_id[pid]
        if p['parent']:
            base = get(p['parent'])
            fields = dict(base[0]); mems = {k: dict(v) for k, v in base[1].items()}
        else:
            fields, mems = {}, {}
        fields.update(p['fields'])
        for name, mf in p['mems'].items():
            if mf is None:
                mems.pop(name, None)
            else:
                mems.setdefault(name, {}).update(mf)
        cache[pid] = (fields, mems)
        return cache[pid]

    return {pid: get(pid) for pid in by_id}


def num(v, default=0):
    try:
        return int(v[0], 0)
    except Exception:
        return default


def cmd(mf, key):
    v = mf.get(key)
    if not v or v[0] == 'NULL':
        return None
    s = ' '.join(v)
    return s


def load_bits(path):
    res = {}
    if not os.path.isfile(path):
        return res
    for m in ET.parse(path).getroot():
        sig = m.get('signature', '').lower()
        d = {}
        for k in ('low', 'high', 'ext', 'lock'):
            e = m.find(k)
            names = (e.text or '').split(',') if e is not None else []
            names = (names + [''] * 8)[:8]
            d[k] = names[::-1]          # в xml bit0..bit7, у нас 7..0
        res[sig] = d
    return res


def main():
    conf = open(os.path.join(SRC, 'avrdude.conf'), encoding='utf-8', errors='replace').read()
    ver = re.search(r'avrdude_conf_version\s*=\s*"([^"]+)"', conf)
    parts = resolve(parse(conf))
    bits = load_bits(os.path.join(SRC, 'bits.xml'))
    chips = {}
    skipped = []
    for pid, (f, mems) in sorted(parts.items()):
        if pid.startswith('.'):
            continue
        modes = ' '.join(f.get('prog_modes', []))
        sig = f.get('signature', [])
        if 'PM_ISP' not in modes or len(sig) != 3:
            continue
        sig = tuple(int(x, 0) for x in sig)
        if sig in ((0, 0, 0), (0xFF, 0xFF, 0xFF)):
            skipped.append((pid, 'нет сигнатуры')); continue
        fl = mems.get('flash', {})
        flash = dict(size=num(fl.get('size')), page=num(fl.get('page_size')),
                     read_lo=cmd(fl, 'read_lo'), read_hi=cmd(fl, 'read_hi'),
                     load_lo=cmd(fl, 'loadpage_lo'), load_hi=cmd(fl, 'loadpage_hi'),
                     writepage=cmd(fl, 'writepage'), ext=cmd(fl, 'load_ext_addr'),
                     write_lo=cmd(fl, 'write_lo'), write_hi=cmd(fl, 'write_hi'),
                     delay=num(fl.get('max_write_delay'), 4500))
        paged = flash['load_lo'] and flash['writepage']
        if not (flash['size'] and flash['read_lo'] and flash['read_hi'] and (paged or flash['write_lo'])):
            skipped.append((pid, 'нет ISP-команд flash')); continue
        if not cmd(f, 'pgm_enable'):
            skipped.append((pid, 'нет pgm_enable')); continue
        ee = mems.get('eeprom', {})
        eeprom = None
        if num(ee.get('size')) and cmd(ee, 'read') and (cmd(ee, 'write') or cmd(ee, 'writepage')):
            eeprom = dict(size=num(ee.get('size')), page=num(ee.get('page_size')),
                          read=cmd(ee, 'read'), write=cmd(ee, 'write'),
                          load=cmd(ee, 'loadpage_lo'), writepage=cmd(ee, 'writepage'),
                          delay=num(ee.get('max_write_delay'), 9000))
        fuses = {}
        for mem, key in (('lfuse', 'low'), ('fuse', 'low'), ('hfuse', 'high'), ('efuse', 'ext'), ('lock', 'lock')):
            m = mems.get(mem)
            if m and cmd(m, 'read') and cmd(m, 'write'):
                fuses[key] = dict(mem=mem, read=cmd(m, 'read'), write=cmd(m, 'write'),
                                  mask=num(m.get('bitmask'), 0xFF) & 0xFF, init=num(m.get('initval'), -1),
                                  delay=num(m.get('max_write_delay'), 9000))
        b = bits.get('%02x%02x%02x' % sig)
        if b:
            b = {k: list(v) for k, v in b.items()}
            # SPIEN в bits.xml не назван (чтобы его не трогали) — восстанавливаем позицию
            hm = fuses.get('high', {}).get('mask', 0); lm = fuses.get('low', {}).get('mask', 0)
            if 'high' in fuses and hm & 0x20 and not b['high'][2]:
                b['high'][2] = 'SPIEN'
            elif 'low' in fuses and lm & 0x80 and not b['low'][0] and 'SPIEN' not in sum(b.values(), []):
                b['low'][0] = 'SPIEN'
        name = f.get('desc', [pid])[0]
        clone = pid.startswith(('ata', 'lgt'))   # автомобильные ATA*/китайские LGT — только как доп. имя
        names = sorted(set((chips[sig]['names'] if sig in chips else []) + [name]),
                       key=lambda n: (n.startswith(('ATA', 'LGT')), n))
        if sig in chips and clone and not chips[sig]['id'].startswith(('ata', 'lgt')):
            chips[sig]['names'] = names; chips[sig]['name'] = ' / '.join(names)
            continue
        chips[sig] = dict(id=pid, name=' / '.join(names), names=names, flash=flash, eeprom=eeprom, fuses=fuses,
                          erase=cmd(f, 'chip_erase'), erase_delay=num(f.get('chip_erase_delay'), 9000),
                          pgm_enable=cmd(f, 'pgm_enable'),
                          pollindex=num(f.get('pollindex'), 3), pollvalue=num(f.get('pollvalue'), 0x53),
                          bits=b)
    with open(OUT, 'w', encoding='utf-8') as o:
        o.write('# Сгенерировано gen_chips.py из avrdude.conf %s + bits.xml (AVRDUDESS). Не редактировать.\n'
                % (ver.group(1) if ver else '?'))
        o.write('CHIPS = ')
        o.write(pprint.pformat(chips, width=140, sort_dicts=False))
        o.write('\n')
    print('чипов:', len(chips), ' с именами битов:', sum(1 for c in chips.values() if c['bits']),
          ' с EEPROM:', sum(1 for c in chips.values() if c['eeprom']))
    for s in skipped:
        print('  пропущен', *s)


if __name__ == '__main__':
    main()
