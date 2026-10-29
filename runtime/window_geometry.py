"""Restore the required geometry only after the caller verifies window ownership."""
import time


def restore(window,root,run):
    geometry=window.get_geometry()
    if (geometry.width,geometry.height)!=(880,640):
        run('xdotool','windowsize','--sync',str(window.id),'880','640')
    position=root.translate_coords(window,0,0)
    if (position.x,position.y)!=(200,80):
        run('xdotool','windowmove','--sync',str(window.id),'200','80')
    for _ in range(10):
        geometry=window.get_geometry();position=root.translate_coords(window,0,0)
        if (geometry.width,geometry.height,position.x,position.y)==(880,640,200,80):return
        time.sleep(.1)
    raise RuntimeError('Verified window geometry could not be restored to 880x640 at 200,80')
