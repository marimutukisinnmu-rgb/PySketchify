from __future__ import annotations

"""Fast, internal image-to-strokes renderer for PySketchify."""

from dataclasses import dataclass
import math

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
except ImportError as exc:
    raise ImportError("sketch_renderer requires numpy and Pillow. Install: pip install numpy pillow") from exc

PEN_TYPES = ("●", "■", "▲")

@dataclass(frozen=True)
class PencilSettings:
    width: int = 3
    pen_type: str = "●"
    detail: float = 0.65
    color_strength: float = 0.82
    line_strength: float = 0.88
    analysis_max_size: int = 720
    def normalized(self) -> "PencilSettings":
        return PencilSettings(max(1,min(64,int(self.width))), self.pen_type if self.pen_type in PEN_TYPES else "●",
                              max(.1,min(1.,float(self.detail))), max(0.,min(1.,float(self.color_strength))),
                              max(0.,min(1.,float(self.line_strength))), max(256,min(1280,int(self.analysis_max_size))))

def _stroke(draw, points, width, pen_type, fill):
    if len(points)<2: return
    if pen_type=="■": draw.line(points,fill=fill,width=width,joint="curve"); return
    if pen_type=="▲":
        r=max(1.,width*.5)
        for i in range(0,len(points),max(1,len(points)//8)):
            x,y=points[i]; dx,dy=(points[i+1][0]-x,points[i+1][1]-y) if i+1<len(points) else ((x-points[i-1][0],y-points[i-1][1]) if i else (1.,0.))
            length=math.hypot(dx,dy) or 1.; ux,uy=dx/length,dy/length; px,py=-uy,ux
            tip=(x+ux*r*1.7,y+uy*r*1.7); left=(x-ux*r+px*r,y-uy*r+py*r); right=(x-ux*r-px*r,y-uy*r-py*r)
            draw.polygon((tip,left,right),fill=fill)
        return
    draw.line(points,fill=fill,width=width,joint="curve"); radius=max(1,width//2)
    for x,y in (points[0],points[-1]): draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=fill)

def _edge_strokes(gray, settings, sx, sy):
    from gpu_backend import edge_magnitude
    mag=edge_magnitude(gray)
    threshold=np.percentile(mag,88.-18.*settings.detail); ys,xs=np.where(mag>=max(8.,threshold))
    max_points=max(500,int(gray.size*(.002+.004*settings.detail)))
    if len(xs)>max_points:
        stride=max(1,len(xs)//max_points); xs,ys=xs[::stride],ys[::stride]
    strokes=[]; step=max(1,int(2.5-settings.detail*1.5))
    for x,y in zip(xs[::step],ys[::step]):
        gx=float(mag[y,x]); gy=float(mag[y,x])
        # Direction is estimated locally on CPU from neighboring luminance.
        left=float(gray[y,max(0,x-1)]); right=float(gray[y,min(gray.shape[1]-1,x+1)])
        up=float(gray[max(0,y-1),x]); down=float(gray[min(gray.shape[0]-1,y+1),x])
        dx=right-left; dy=down-up; length=math.hypot(dx,dy) or 1.; tx,ty=-dy/length,dx/length; span=2.+settings.detail*5.
        p1=((x-tx*span)*sx,(y-ty*span)*sy); p2=((x+tx*span)*sx,(y+ty*span)*sy)
        strength=min(255,int(55+min(1.,gx/160.)*170*settings.line_strength)); strokes.append((p1,p2,strength))
    return strokes

def _color_dabs(analysis, settings, sx, sy):
    arr=np.asarray(analysis,dtype=np.uint8); h,w=arr.shape[:2]; step=max(3,int(7-settings.detail*4)); dabs=[]
    for y in range(step//2,h,step):
        for x in range(step//2,w,step):
            r,g,b=map(int,arr[y,x,:3])
            if settings.color_strength<1.: r=int(255+(r-255)*settings.color_strength); g=int(255+(g-255)*settings.color_strength); b=int(255+(b-255)*settings.color_strength)
            dabs.append(((x*sx,y*sy),(r,g,b,210)))
    return dabs

def render_frame(frame,index,width,height,settings):
    settings=settings.normalized(); expected=width*height*3
    if len(frame)!=expected: raise ValueError(f"invalid RGB frame size: {len(frame)} != {expected}")
    source=Image.frombytes("RGB",(width,height),frame); scale=min(1.,settings.analysis_max_size/max(width,height)); aw=max(1,int(width*scale)); ah=max(1,int(height*scale)); analysis=source.resize((aw,ah),Image.Resampling.BILINEAR)
    gray=np.asarray(analysis.convert("L").filter(ImageFilter.GaussianBlur(radius=.45)),dtype=np.uint8); base=analysis.copy()
    if settings.color_strength<1.: base=Image.blend(Image.new("RGB",analysis.size,(255,255,255)),base,settings.color_strength)
    canvas=base.convert("RGBA"); draw=ImageDraw.Draw(canvas,"RGBA"); sx,sy=width/aw,height/ah; line_width=max(1,int(settings.width/max(.5,scale)))
    for p1,p2,strength in _edge_strokes(gray,settings,sx,sy): _stroke(draw,[p1,p2],line_width,settings.pen_type,(25,25,25,strength))
    if settings.color_strength>.05:
        for (x,y),fill in _color_dabs(analysis,settings,sx,sy):
            _stroke(draw,[(x-line_width*.35,y),(x+line_width*.35,y)],line_width,settings.pen_type,fill)
    return canvas.convert("RGB").resize((width,height),Image.Resampling.BICUBIC).tobytes()

def make_processor(settings):
    normalized=settings.normalized()
    def processor(frame,index,width,height): return render_frame(frame,index,width,height,normalized)
    processor.pencil_settings=normalized
    return processor
