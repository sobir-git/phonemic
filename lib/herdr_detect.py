"""Associate normal Herdr launches with X11 windows without matching their titles."""
import json
import os
from pathlib import Path
import re
import subprocess

PROPERTY = '_PHONEMIC_HERDR_CLIENTS'


def process_identity(pid):
    try:
        p = Path('/proc') / str(pid)
        stat = (p/'stat').read_text().rsplit(')',1)[1].split()
        return {'pid':int(pid),'start':stat[19],'foreground':int(stat[4]) != 0 and stat[2] == stat[5],
                'name':(p/'comm').read_text().strip()}
    except (OSError,ValueError,IndexError):
        return None


def descendant_of(pid, ancestor):
    for _ in range(64):
        if pid == ancestor:return True
        try:
            stat=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
            pid=int(stat[1])
            if pid <= 1:return False
        except (OSError,ValueError,IndexError):return False
    return False


def active_tab(d, window):
    from Xlib import X
    def text(name):
        p=window.get_full_property(d.intern_atom(name),X.AnyPropertyType)
        return p.value.decode() if p else ''
    bus,path=text('_GTK_UNIQUE_BUS_NAME'),text('_GTK_WINDOW_OBJECT_PATH')
    if not path.startswith('/org/gnome/Terminal/window/'):
        return None
    try:
        output=subprocess.run(['gdbus','call','--session','--dest',bus,'--object-path',path,
            '--method','org.gtk.Actions.Describe','active-tab'],capture_output=True,text=True,timeout=.5,check=True).stdout
        match=re.search(r'<(-?\d+)>',output)
        return int(match[1]) if match else None
    except (OSError,subprocess.SubprocessError):
        return None


def records(d, window):
    from Xlib import X
    prop=window.get_full_property(d.intern_atom(PROPERTY),X.AnyPropertyType)
    try:
        data=json.loads(prop.value) if prop and len(prop.value)<16384 else []
        return data if isinstance(data,list) else []
    except (ValueError,TypeError):
        return []


def register(pid=None, window_id=None):
    from Xlib import X, Xatom, display
    pid=pid or os.getpid()
    identity=process_identity(pid)
    if not identity or not identity['foreground']:
        return
    d=display.Display()
    try:
        if window_id is None:
            prop=d.screen().root.get_full_property(d.intern_atom('_NET_ACTIVE_WINDOW'),X.AnyPropertyType)
            if not prop or not len(prop.value):return
            window_id=int(prop.value[0])
        w=d.create_resource_object('window',window_id)
        owner=w.get_full_property(d.intern_atom('_NET_WM_PID'),X.AnyPropertyType)
        if not owner or not len(owner.value) or not descendant_of(pid,int(owner.value[0])):
            return
        tab=active_tab(d,w)
        existing=[r for r in records(d,w) if isinstance(r,dict) and process_identity(r.get('pid'))]
        existing=[r for r in existing if r.get('tab')!=tab]
        existing.append({'pid':pid,'start':identity['start'],'tab':tab})
        w.change_property(d.intern_atom(PROPERTY),Xatom.STRING,8,json.dumps(existing[-32:]).encode())
        d.sync()
    finally:
        d.close()


def detected(d, window):
    candidates=records(d,window)
    if not candidates:return False
    tab=active_tab(d,window)
    for record in candidates:
        if not isinstance(record,dict) or record.get('tab')!=tab:continue
        p=process_identity(record.get('pid'))
        if p and p['name']=='herdr' and p['foreground'] and p['start']==record.get('start'):
            return True
    return False


def launch():
    import sys
    binary=Path(__file__).resolve().parent.parent/'herdr-bin/herdr'
    if not binary.is_file():
        raise SystemExit('PhoneMic Herdr launcher: original binary is missing')
    if os.isatty(0) and os.environ.get('DISPLAY'):
        try:register()
        except Exception:pass  # Desktop integration never prevents the terminal application starting.
    os.execv(str(binary),[str(binary),*sys.argv[1:]])


if __name__=='__main__':launch()
