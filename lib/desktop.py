"""X11 window inventory and targeted compositor previews.

Metadata requests intentionally do not import Pillow or capture screenshots.
Preview capture is an explicit, bounded operation for one window at a time.
"""
import base64
import io
import json
import os
import re
import sys


PREVIEW_WIDTH = 192
PREVIEW_HEIGHT = 120
PREVIEW_MAX_PIXELS = 8_000_000
PREVIEW_MAX_BASE64 = 16 * 1024
WINDOW_ID = re.compile(r"[0-9]{1,10}")


def _x11():
    if os.environ.get('XDG_SESSION_TYPE') == 'wayland' or not os.environ.get('DISPLAY'):
        raise RuntimeError('App switching requires an X11 desktop session')
    try:
        from Xlib import X, display
    except ImportError:
        raise RuntimeError('Install python-xlib for desktop window metadata') from None
    return X, display


def _prop(window, atom, X):
    result = window.get_full_property(atom, X.AnyPropertyType)
    return result.value if result else []


def _atoms(d):
    return {name: d.intern_atom(name) for name in (
        '_NET_CLIENT_LIST', '_NET_ACTIVE_WINDOW', '_NET_WM_STATE',
        '_NET_WM_STATE_SKIP_TASKBAR', '_NET_WM_NAME')}


def _window_metadata(d, window, atoms, active, detect_herdr=False):
    from Xlib import X
    states = _prop(window, atoms['_NET_WM_STATE'], X)
    if atoms['_NET_WM_STATE_SKIP_TASKBAR'] in states:
        return None
    title = (window.get_full_text_property(atoms['_NET_WM_NAME']) or
             window.get_wm_name() or 'Untitled window')
    classes = window.get_wm_class() or ['Application']
    item = {'id': str(window.id), 'title': str(title)[:200],
            'app': str(classes[-1])[:80], 'active': window.id == active,
            'preview': None}
    try:
        geometry = window.get_geometry()
        item['width'], item['height'] = geometry.width, geometry.height
    except Exception:
        item['width'], item['height'] = 800, 600
    if detect_herdr:
        try:
            from .herdr_detect import detected
        except ImportError:
            from herdr_detect import detected
        try:
            item['herdr'] = detected(d, window)
        except Exception:
            item['herdr'] = False
    return item


def _open_display():
    X, display = _x11()
    d = display.Display()
    atoms = _atoms(d)
    root = d.screen().root
    ids = _prop(root, atoms['_NET_CLIENT_LIST'], X)
    active_values = _prop(root, atoms['_NET_ACTIVE_WINDOW'], X)
    active = int(active_values[0]) if active_values else 0
    return X, d, atoms, [int(wid) for wid in list(ids)[:48]], active


def snapshot(previews=False):
    """Return bounded window metadata without doing screenshot work."""
    del previews  # Kept for callers that used the old function signature.
    X, d, atoms, ids, active = _open_display()
    try:
        windows = []
        for wid in ids:
            try:
                window = d.create_resource_object('window', wid)
                item = _window_metadata(d, window, atoms, active)
                if item:
                    windows.append(item)
            except Exception:
                continue  # A window may close while its metadata is read.
        return {'windows': windows, 'previewSupported': bool(d.has_extension('Composite'))}
    finally:
        d.close()


def active_context():
    """Read only the current active window for fresh input validation."""
    X, d, atoms, ids, active = _open_display()
    del X, ids
    try:
        if not active:
            return {}
        window = d.create_resource_object('window', active)
        item = _window_metadata(d, window, atoms, active, detect_herdr=True)
        if not item:
            return {}
        return {key: item[key] for key in ('id', 'app', 'herdr', 'width', 'height')}
    except Exception:
        return {}
    finally:
        d.close()


def preview(window_id):
    """Capture one current, viewable X11 client window as a small JPEG."""
    if not WINDOW_ID.fullmatch(window_id):
        raise RuntimeError('Invalid window selection')
    X, d, atoms, ids, active = _open_display()
    pixmap = None
    try:
        wid = int(window_id)
        if wid not in ids or not d.has_extension('Composite'):
            return {'window': window_id, 'preview': None}
        window = d.create_resource_object('window', wid)
        item = _window_metadata(d, window, atoms, active)
        if not item or window.get_attributes().map_state != X.IsViewable:
            return {'window': window_id, 'preview': None}
        try:
            from PIL import Image
        except ImportError:
            raise RuntimeError('Install Pillow for application previews') from None
        pixmap = window.composite_name_window_pixmap()
        geometry = pixmap.get_geometry()
        width, height = geometry.width, geometry.height
        if (geometry.depth not in (24, 32) or width <= 0 or height <= 0 or
                width * height > PREVIEW_MAX_PIXELS):
            return {'window': window_id, 'preview': None}
        raw = pixmap.get_image(0, 0, width, height, X.ZPixmap, 0xffffffff)
        if len(raw.data) != width * height * 4 or d.display.info.image_byte_order != X.LSBFirst:
            return {'window': window_id, 'preview': None}
        image = Image.frombytes('RGB', (width, height), raw.data, 'raw', 'BGRX')
        image.thumbnail((PREVIEW_WIDTH, PREVIEW_HEIGHT))
        for quality in (45, 30):
            out = io.BytesIO()
            image.save(out, format='JPEG', quality=quality, optimize=True)
            encoded = base64.b64encode(out.getvalue()).decode()
            if len(encoded) <= PREVIEW_MAX_BASE64:
                return {'window': window_id, 'preview': encoded}
        return {'window': window_id, 'preview': None}
    except RuntimeError:
        raise
    except Exception:
        return {'window': window_id, 'preview': None}
    finally:
        if pixmap:
            pixmap.free()
        d.close()


def main():
    try:
        if '--context' in sys.argv:
            result = active_context()
        elif '--preview' in sys.argv:
            index = sys.argv.index('--preview')
            if index + 1 >= len(sys.argv):
                raise RuntimeError('Invalid window selection')
            result = preview(sys.argv[index + 1])
        else:
            result = snapshot(previews=False)
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({'error': str(e) if isinstance(e, RuntimeError)
                          else 'Desktop windows are unavailable'}))


if __name__ == '__main__':
    main()
