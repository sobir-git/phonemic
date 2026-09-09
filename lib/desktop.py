"""X11 window inventory and small compositor previews; no images are stored on disk."""
import base64
import io
import json
import os
import sys


def snapshot(previews=True):
    if os.environ.get('XDG_SESSION_TYPE') == 'wayland' or not os.environ.get('DISPLAY'):
        raise RuntimeError('App switching requires an X11 desktop session')
    try:
        from Xlib import X, Xatom, display
        from PIL import Image
    except ImportError:
        raise RuntimeError('Install python-xlib and Pillow for app previews') from None
    d = display.Display()
    try:
        root = d.screen().root
        def prop(window, name):
            result = window.get_full_property(d.intern_atom(name), X.AnyPropertyType)
            return result.value if result else []
        ids = prop(root, '_NET_CLIENT_LIST')
        active = prop(root, '_NET_ACTIVE_WINDOW')
        active = int(active[0]) if len(active) else 0
        result = []
        for wid in list(ids)[:48]:
            try:
                w = d.create_resource_object('window', int(wid))
                states = prop(w, '_NET_WM_STATE')
                if d.intern_atom('_NET_WM_STATE_SKIP_TASKBAR') in states:
                    continue
                title = w.get_full_text_property(d.intern_atom('_NET_WM_NAME')) or w.get_wm_name() or 'Untitled window'
                classes = w.get_wm_class() or ['Application']
                item = {'id':str(int(wid)), 'title':str(title)[:200], 'app':str(classes[-1])[:80],
                        'active':int(wid)==active, 'preview':None}
                try:
                    from .herdr_detect import detected
                except ImportError:
                    from herdr_detect import detected
                item['herdr']=detected(d,w)
                geometry=w.get_geometry();item['width']=geometry.width;item['height']=geometry.height
                if previews and len(result) < 24 and w.get_attributes().map_state == X.IsViewable and d.has_extension('Composite'):
                    pixmap = None
                    try:
                        pixmap = w.composite_name_window_pixmap()
                        geometry = pixmap.get_geometry()
                        width, height = geometry.width, geometry.height
                        if geometry.depth in (24,32) and 0 < width*height <= 16000000:
                            raw = pixmap.get_image(0,0,width,height,X.ZPixmap,0xffffffff)
                            if len(raw.data) == width*height*4 and d.display.info.image_byte_order == X.LSBFirst:
                                im = Image.frombytes('RGB',(width,height),raw.data,'raw','BGRX')
                                im.thumbnail((280,176))
                                out = io.BytesIO();im.save(out,format='JPEG',quality=55)
                                item['preview'] = base64.b64encode(out.getvalue()).decode()
                    except Exception:
                        pass
                    finally:
                        if pixmap:
                            pixmap.free()
                result.append(item)
            except Exception:
                continue  # A window may close while its metadata is being read.
        return {'windows':result}
    finally:
        d.close()


if __name__ == '__main__':
    try:
        print(json.dumps(snapshot(previews='--names' not in sys.argv)))
    except Exception as e:
        print(json.dumps({'error':str(e) if isinstance(e,RuntimeError) else 'Desktop windows are unavailable'}))
