"""
Pixel Matrix Studio
Control por Bluetooth LE para paneles LED iPixel Color / LED_BLE.

Usa el protocolo iPIXEL real (pypixelcolor): ventanas de 12 KB con CRC32 y
confirmacion por ACK. Los intentos caseros de trama fallaban por no llevar
CRC ni esperar el ACK de cada ventana.

Panel de referencia: LED_BLE_098a4c32 (device type 128 -> 64x64).
"""

import io
import json
import os
import threading
import time

import tkinter as tk
from tkinter import ttk, filedialog, colorchooser, messagebox

from PIL import Image, ImageTk, ImageEnhance, ImageFilter

try:
    from pypixelcolor import Client
except ImportError:
    Client = None

try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None

NAME_PREFIXES = ("LED_BLE", "IDM-", "IPIXEL")
SERVICIO = "0000fa00"

CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "PixelMatrixStudio.json"
)

ANIMACIONES = [("Fijo", 0), ("Desplazar izquierda", 1), ("Desplazar derecha", 2),
               ("Parpadeo", 5), ("Aparecer", 6), ("Continuo", 7)]
ORIENTACIONES = [("Normal", 0), ("90°", 1), ("180°", 2), ("270°", 3)]


# --------------------------------------------------------------------------
def escanear(timeout=8.0):
    """Busca paneles compatibles. Devuelve [(nombre, mac, rssi), ...]."""
    import asyncio

    async def _go():
        out = []
        for dev, adv in (await BleakScanner.discover(timeout=timeout,
                                                     return_adv=True)).values():
            nombre = adv.local_name or dev.name or ""
            uuids = " ".join(adv.service_uuids or []).lower()
            punt = 2 if any(nombre.upper().startswith(p) for p in NAME_PREFIXES) else (
                1 if SERVICIO in uuids else 0)
            if punt:
                out.append((punt, nombre or "<sin nombre>", dev.address, adv.rssi))
        out.sort(key=lambda t: (-t[0], -(t[3] if t[3] is not None else -999)))
        return [(n, a, r) for _, n, a, r in out]

    return asyncio.run(_go())


def png_bytes(img):
    b = io.BytesIO()
    img.convert("RGB").save(b, format="PNG")
    return b.getvalue()


def png_hex(img):
    """send_image_hex espera la cadena hexadecimal, no los bytes crudos."""
    return png_bytes(img).hex()


def es_pixel_art(img):
    """Paleta reducida = pixel art. Las fotos superan de largo este umbral."""
    colores = img.convert("RGB").getcolors(4096)
    return colores is not None and len(colores) <= 256


def filtro_auto(img, destino=64):
    """Elige el remuestreo segun el tipo de imagen Y su tamano.

    Con pixel art hay dos casos muy distintos:
      - ya esta cerca del tamano final (100x100, 128x128): NEAREST conserva
        el pixel duro y queda perfecto.
      - esta renderizado enorme (400x400, 1500x1500): NEAREST tomaria 1 pixel
        de cada 6 o 23 y destroza el dibujo. BOX promedia el area y respeta
        las formas.
    """
    if not es_pixel_art(img):
        return Image.LANCZOS
    return Image.NEAREST if max(img.size) <= destino * 2 else Image.BOX


FILTROS = {"auto": None, "box": Image.BOX,
           "nearest": Image.NEAREST, "lanczos": Image.LANCZOS}
NOMBRE_FILTRO = {Image.NEAREST: "píxel duro", Image.BOX: "área",
                 Image.LANCZOS: "suavizado", Image.HAMMING: "hamming"}


def tiene_transparencia(img):
    """True si la imagen lleva canal alfa o un color marcado como transparente."""
    return (img.mode in ("RGBA", "LA")
            or (img.mode == "P" and "transparency" in img.info)
            or "transparency" in img.info)


def aplanar(img, fondo=(0, 0, 0)):
    """Compone sobre un color solido. Sin esto, lo transparente acababa en negro."""
    if not tiene_transparencia(img):
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    lienzo = Image.new("RGBA", rgba.size, tuple(fondo) + (255,))
    return Image.alpha_composite(lienzo, rgba).convert("RGB")


def fit_image(img, w, h, modo="cubrir", filtro=None, fondo=(0, 0, 0)):
    """Adapta la imagen a w x h. 'filtro' None = decidir automaticamente."""
    if filtro is None:
        filtro = filtro_auto(img)
    img = aplanar(img, fondo)
    if modo == "cubrir":
        rel_o, rel_d = img.width / img.height, w / h
        if rel_o > rel_d:
            nw = int(img.height * rel_d)
            img = img.crop(((img.width - nw) // 2, 0, (img.width + nw) // 2, img.height))
        else:
            nh = int(img.width / rel_d)
            img = img.crop((0, (img.height - nh) // 2, img.width, (img.height + nh) // 2))
        return img.resize((w, h), filtro)
    if modo == "estirar":
        return img.resize((w, h), filtro)
    img = img.copy()
    img.thumbnail((w, h), filtro)
    lienzo = Image.new("RGB", (w, h), tuple(fondo))
    lienzo.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return lienzo


def realzar(img, nitidez=0, colores=0):
    """Devuelve definicion aparente tras reducir de tamano.

    'nitidez' 0-100 se mapea a una mascara de enfoque; 'colores' > 0 aplana la
    paleta, que en un panel LED se lee mucho mas limpio.
    """
    if nitidez:
        img = img.filter(ImageFilter.UnsharpMask(
            radius=0.8, percent=int(nitidez * 3), threshold=0))
    if colores:
        img = img.quantize(colors=int(colores), method=Image.MEDIANCUT,
                           dither=Image.NONE).convert("RGB")
    return img


def resolucion_logica(img, muestras=64):
    """Estima cuantos pixeles 'de verdad' tiene un pixel art escalado.

    Busca cada cuantas filas/columnas cambia el contenido: ese es el tamano de
    bloque. Sirve para avisar cuando la imagen trae mas detalle del que cabe.
    """
    from math import gcd
    from functools import reduce

    def bloque(im):
        w, h = im.size
        px = im.load()
        cols = range(0, w, max(1, w // muestras))
        cambios, ant = [], None
        for y in range(h):
            fila = tuple(px[x, y] for x in cols)
            if ant is not None and fila != ant:
                cambios.append(y)
            ant = fila
        difs = [b - a for a, b in zip(cambios, cambios[1:]) if b > a]
        return reduce(gcd, difs) if difs else 1

    try:
        rgb = img.convert("RGB")
        by = bloque(rgb)
        bx = bloque(rgb.transpose(Image.TRANSPOSE))
        return max(1, rgb.width // max(1, bx)), max(1, rgb.height // max(1, by))
    except Exception:
        return img.size


def preparar_gif(path, w, h, modo, filtro, nitidez=0, colores=0,
                 fondo=(0, 0, 0)):
    """Reescala un GIF entero y lo reempaqueta SIN parpadeo.

    Dos detalles que costaron sangre:
      - la paleta se calcula UNA vez para toda la animacion. Cuantizando cada
        fotograma por separado, cada uno elegia colores ligeramente distintos
        y las zonas quietas parpadeaban.
      - disposal=1 (dejar el fotograma anterior) en vez de 2 (restaurar al
        fondo), que provocaba destellos negros entre cuadros.
    """
    src = Image.open(path)
    frames, duraciones = [], []
    try:
        while True:
            f = fit_image(src, w, h, modo, filtro, fondo)
            if nitidez:
                f = f.filter(ImageFilter.UnsharpMask(
                    radius=0.8, percent=int(nitidez * 3), threshold=0))
            frames.append(f.convert("RGB"))
            duraciones.append(src.info.get("duration", 80))
            src.seek(src.tell() + 1)
    except EOFError:
        pass

    # Paleta unica: se cuantiza el MONTAJE de todos los fotogramas de una sola
    # vez y luego se recorta. Cuantizar cuadro a cuadro contra una paleta
    # comun todavia dejaba parpadeo (665 px de cambio por cuadro frente a los
    # 428 del original); asi baja a 425, es decir, ninguno anadido.
    n_pal = int(colores) if colores else 128
    montaje = Image.new("RGB", (w, h * len(frames)))
    for i, f in enumerate(frames):
        montaje.paste(f, (0, i * h))
    mont_p = montaje.quantize(colors=n_pal, method=Image.MEDIANCUT,
                              dither=Image.NONE)
    frames = [mont_p.crop((0, i * h, w, (i + 1) * h)) for i in range(len(frames))]

    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   loop=0, duration=duraciones, disposal=1, optimize=False)
    return buf.getvalue(), len(frames)


def enhance(img, brillo=1.0, contraste=1.0, saturacion=1.0):
    if brillo != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brillo)
    if contraste != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contraste)
    if saturacion != 1.0:
        img = ImageEnhance.Color(img).enhance(saturacion)
    return img


class RegionPicker(tk.Toplevel):
    def __init__(self, master, callback):
        super().__init__(master)
        self.callback = callback
        self.attributes("-alpha", 0.28)
        self.attributes("-topmost", True)
        self.overrideredirect(True)
        try:
            import mss
            with mss.mss() as s:
                m = s.monitors[0]
            self.geometry("{}x{}+{}+{}".format(m["width"], m["height"],
                                               m["left"], m["top"]))
            self.origen = (m["left"], m["top"])
        except Exception:
            self.geometry("{}x{}+0+0".format(self.winfo_screenwidth(),
                                             self.winfo_screenheight()))
            self.origen = (0, 0)
        self.configure(bg="black")
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.ini = self.rect = None
        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def _down(self, e):
        self.ini = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline="#4ade80", width=3)

    def _move(self, e):
        if self.ini:
            self.canvas.coords(self.rect, self.ini[0], self.ini[1], e.x, e.y)

    def _up(self, e):
        if not self.ini:
            return
        x1, y1 = self.ini
        x, y = min(x1, e.x), min(y1, e.y)
        w, h = abs(e.x - x1), abs(e.y - y1)
        self.destroy()
        if w > 8 and h > 8:
            self.callback({"left": self.origen[0] + x, "top": self.origen[1] + y,
                           "width": w, "height": h})


# --------------------------------------------------------------------------
BG, BG2, FG, ACC, DIM = "#14161c", "#1d212b", "#e7ebf3", "#4ade80", "#8b93a7"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pixel Matrix Studio")
        self.configure(bg=BG)
        self.geometry("1040x760")
        self.minsize(960, 700)

        self._cm = None          # context manager del Client
        self.dev = None          # Client conectado
        self.W = self.H = 64
        self.cancelar = threading.Event()
        self.worker = None
        self.region = None
        self.current_img = None
        self.dispositivos = []
        # Que se esta viendo ahora mismo: ("imagen"|"gif", ruta). Sin esto, los
        # controles de escalado repintaban siempre la ultima imagen fija.
        self.fuente = None

        # Ajustes de escalado, globales a imagen / GIF / video / pantalla
        self.modo_fit = tk.StringVar(value="cubrir")
        self.v_escalado = tk.StringVar(value="auto")
        self.v_nitidez = tk.IntVar(value=45)
        self.v_colores = tk.StringVar(value="original")
        self.fondo = (0, 0, 0)   # color bajo las zonas transparentes

        self._estilo()
        self._construir()
        self._cargar_config()
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

    def _estilo(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG, fieldbackground=BG2)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=BG2)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Card.TLabel", background=BG2, foreground=FG)
        s.configure("Dim.TLabel", background=BG, foreground=DIM)
        s.configure("CardDim.TLabel", background=BG2, foreground=DIM)
        s.configure("TButton", background="#2b3242", foreground=FG,
                    borderwidth=0, padding=7, font=("Segoe UI", 9))
        s.map("TButton", background=[("active", "#3a4256")])
        s.configure("Accent.TButton", background=ACC, foreground="#06210f",
                    font=("Segoe UI Semibold", 9))
        s.map("Accent.TButton", background=[("active", "#6ee79b")])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=BG, foreground=DIM,
                    padding=(15, 9), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", BG2)],
              foreground=[("selected", FG)])
        for w in ("TRadiobutton", "TCheckbutton"):
            s.configure(w, background=BG2, foreground=FG)
            s.map(w, background=[("active", BG2)])
        s.configure("TScale", background=BG2)
        s.configure("TCombobox", fieldbackground="#0a0c10", background="#2b3242")

    def _construir(self):
        cab = ttk.Frame(self, padding=(16, 12, 16, 8))
        cab.pack(fill="x")
        ttk.Label(cab, text="Pixel Matrix Studio",
                  font=("Segoe UI Semibold", 15)).pack(side="left")
        self.lbl_estado = ttk.Label(cab, text="  desconectado", style="Dim.TLabel")
        self.lbl_estado.pack(side="left", padx=(12, 0))
        self.btn_conn = ttk.Button(cab, text="Conectar", style="Accent.TButton",
                                   command=self.toggle_conexion)
        self.btn_conn.pack(side="right", padx=4)
        self.cmb = ttk.Combobox(cab, width=36, state="readonly")
        self.cmb.pack(side="right", padx=6)
        ttk.Button(cab, text="Buscar", command=self.buscar).pack(side="right")

        cuerpo = ttk.Frame(self, padding=(16, 4, 16, 8))
        cuerpo.pack(fill="both", expand=True)

        izq = ttk.Frame(cuerpo, style="Card.TFrame", padding=14)
        izq.pack(side="left", fill="y")
        self.lbl_prev = ttk.Label(izq, text="Vista previa", style="Card.TLabel")
        self.lbl_prev.pack(anchor="w", pady=(0, 8))
        self.canvas = tk.Canvas(izq, width=320, height=320, bg="#0a0c10",
                                highlightthickness=1, highlightbackground="#2b3242")
        self.canvas.pack()
        self.lbl_info = ttk.Label(izq, text="—", style="CardDim.TLabel",
                                  font=("Consolas", 8), justify="left")
        self.lbl_info.pack(anchor="w", pady=(10, 0))

        self.barra = ttk.Progressbar(izq, length=320, mode="indeterminate")
        self.barra.pack(pady=(8, 2))
        self.lbl_tarea = ttk.Label(izq, text="en reposo", style="CardDim.TLabel")
        self.lbl_tarea.pack(anchor="w")

        ttk.Separator(izq, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(izq, text="Brillo del panel", style="Card.TLabel").pack(anchor="w")
        self.v_brillo_hw = tk.IntVar(value=80)
        fb = ttk.Frame(izq, style="Card.TFrame")
        fb.pack(fill="x")
        ttk.Scale(fb, from_=1, to=100, variable=self.v_brillo_hw,
                  orient="horizontal", command=self._brillo_hw).pack(
            side="left", fill="x", expand=True)
        self.lbl_bhw = ttk.Label(fb, text="80", style="CardDim.TLabel", width=4)
        self.lbl_bhw.pack(side="left")

        ttk.Separator(izq, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(izq, text="Escalado", style="Card.TLabel").pack(anchor="w")
        ttk.Label(izq, text="se aplica a imagen, GIF, vídeo y pantalla",
                  style="CardDim.TLabel").pack(anchor="w", pady=(0, 4))
        for txt, val in (("Automático", "auto"),
                         ("Pixel art nativo  ·  ≤128 px", "nearest"),
                         ("Pixel art grande  ·  400 px+", "box"),
                         ("Foto — suavizado", "lanczos")):
            ttk.Radiobutton(izq, text=txt, value=val, variable=self.v_escalado,
                            command=self.refrescar_imagen).pack(anchor="w")

        ttk.Label(izq, text="Encaje", style="Card.TLabel").pack(anchor="w",
                                                                pady=(10, 2))
        fe = ttk.Frame(izq, style="Card.TFrame")
        fe.pack(fill="x")
        for m in ("cubrir", "ajustar", "estirar"):
            ttk.Radiobutton(fe, text=m, value=m, variable=self.modo_fit,
                            command=self.refrescar_imagen).pack(side="left", padx=(0, 8))

        fn = ttk.Frame(izq, style="Card.TFrame")
        fn.pack(fill="x", pady=(10, 0))
        ttk.Label(fn, text="Nitidez", style="Card.TLabel", width=8).pack(side="left")
        ttk.Scale(fn, from_=0, to=100, variable=self.v_nitidez, orient="horizontal",
                  command=self._cambio_realce).pack(side="left", fill="x", expand=True)
        self.lbl_nit = ttk.Label(fn, text="45", style="CardDim.TLabel", width=5)
        self.lbl_nit.pack(side="left")

        fq = ttk.Frame(izq, style="Card.TFrame")
        fq.pack(fill="x", pady=(4, 0))
        ttk.Label(fq, text="Colores", style="Card.TLabel", width=8).pack(side="left")
        cb = ttk.Combobox(fq, textvariable=self.v_colores, width=10, state="readonly",
                          values=["original", "64", "32", "20", "16", "8"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.refrescar_imagen())

        ff = ttk.Frame(izq, style="Card.TFrame")
        ff.pack(fill="x", pady=(8, 0))
        ttk.Label(ff, text="Fondo", style="Card.TLabel", width=8).pack(side="left")
        ttk.Button(ff, text="Elegir…", width=8,
                   command=self.elegir_fondo).pack(side="left")
        self.sw_fondo = tk.Canvas(ff, width=26, height=20, bg="#000000",
                                  highlightthickness=1,
                                  highlightbackground="#3a4256")
        self.sw_fondo.pack(side="left", padx=6)
        ttk.Button(ff, text="Negro", width=6,
                   command=lambda: self.set_fondo((0, 0, 0))).pack(side="left")
        self.lbl_alfa = ttk.Label(izq, text="", style="CardDim.TLabel",
                                  wraplength=320, justify="left")
        self.lbl_alfa.pack(anchor="w", pady=(4, 0))

        rap = ttk.Frame(izq, style="Card.TFrame")
        rap.pack(fill="x", pady=(12, 0))
        ttk.Button(rap, text="Encender", command=lambda: self.simple(
            "set_power", True)).pack(side="left", padx=2)
        ttk.Button(rap, text="Apagar", command=lambda: self.simple(
            "set_power", False)).pack(side="left", padx=2)
        ttk.Button(rap, text="Detener", command=self.detener).pack(side="left", padx=2)

        der = ttk.Frame(cuerpo)
        der.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.nb = ttk.Notebook(der)
        self.nb.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", self._cambio_pestana)
        self._tab_imagen()
        self._tab_gif()
        self._tab_video()
        self._tab_texto()
        self._tab_panel()

        cons = ttk.Frame(self, padding=(16, 0, 16, 12))
        cons.pack(fill="x")
        self.txt = tk.Text(cons, height=6, bg="#0a0c10", fg="#9aa4bb",
                           relief="flat", font=("Consolas", 8), wrap="word")
        self.txt.pack(fill="x")
        self.txt.configure(state="disabled")

    def _card(self, t):
        f = ttk.Frame(self.nb, style="Card.TFrame", padding=16)
        self.nb.add(f, text=t)
        return f

    def _tab_imagen(self):
        f = self._card("Imagen")
        self.ruta_img = tk.StringVar(value="ningún archivo elegido")
        fila = ttk.Frame(f, style="Card.TFrame")
        fila.pack(fill="x")
        ttk.Button(fila, text="Elegir imagen…",
                   command=self.elegir_imagen).pack(side="left")
        ttk.Label(fila, textvariable=self.ruta_img, style="Card.TLabel",
                  width=42).pack(side="left", padx=10)

        self.v_b = tk.DoubleVar(value=1.0)
        self.v_c = tk.DoubleVar(value=1.0)
        self.v_s = tk.DoubleVar(value=1.25)
        for t, v in (("Brillo", self.v_b), ("Contraste", self.v_c),
                     ("Saturación", self.v_s)):
            fl = ttk.Frame(f, style="Card.TFrame")
            fl.pack(fill="x", pady=3)
            ttk.Label(fl, text=t, style="Card.TLabel", width=11).pack(side="left")
            ttk.Scale(fl, from_=0.2, to=2.5, variable=v, orient="horizontal",
                      command=lambda e: self.refrescar_imagen()).pack(
                side="left", fill="x", expand=True)

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=(16, 12))
        self.v_guardar_img = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Guardar en el panel (sobrevive al reinicio)",
                        variable=self.v_guardar_img).pack(anchor="w")
        fs = ttk.Frame(f, style="Card.TFrame")
        fs.pack(fill="x", pady=(6, 0))
        ttk.Label(fs, text="en el slot", style="Card.TLabel", width=11).pack(side="left")
        self.v_slot_img = tk.IntVar(value=1)
        ttk.Combobox(fs, textvariable=self.v_slot_img, width=6, state="readonly",
                     values=list(range(1, 11))).pack(side="left")
        ttk.Label(fs, text="de los 10 huecos de memoria del panel",
                  style="CardDim.TLabel").pack(side="left", padx=10)

        ttk.Button(f, text="Enviar al panel", style="Accent.TButton",
                   command=self.enviar_imagen).pack(anchor="w", pady=(16, 0))
        ttk.Label(f, style="CardDim.TLabel", wraplength=430, justify="left",
                  text="Sin guardar, la imagen se pierde al apagar el panel. "
                       "Guardada en un slot queda en su memoria interna y la "
                       "vuelve a mostrar al encenderlo."). pack(anchor="w",
                                                                pady=(12, 0))

    def _tab_gif(self):
        f = self._card("GIF")
        self.ruta_gif = tk.StringVar(value="ningún archivo elegido")
        fila = ttk.Frame(f, style="Card.TFrame")
        fila.pack(fill="x")
        ttk.Button(fila, text="Elegir GIF…", command=self.elegir_gif).pack(side="left")
        ttk.Label(fila, textvariable=self.ruta_gif, style="Card.TLabel",
                  width=42).pack(side="left", padx=10)
        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=(16, 12))
        self.v_guardar_gif = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Guardar en el panel (sobrevive al reinicio)",
                        variable=self.v_guardar_gif).pack(anchor="w")
        fs = ttk.Frame(f, style="Card.TFrame")
        fs.pack(fill="x", pady=(6, 0))
        ttk.Label(fs, text="en el slot", style="Card.TLabel", width=11).pack(side="left")
        self.v_slot_gif = tk.IntVar(value=2)
        ttk.Combobox(fs, textvariable=self.v_slot_gif, width=6, state="readonly",
                     values=list(range(1, 11))).pack(side="left")
        ttk.Label(f, style="CardDim.TLabel", wraplength=430, justify="left",
                  text="El GIF se sube entero y lo reproduce el propio panel en "
                       "bucle. Puedes cerrar el programa y sigue animándose."
                  ).pack(anchor="w", pady=(14, 0))
        ttk.Button(f, text="Subir GIF", style="Accent.TButton",
                   command=self.enviar_gif).pack(anchor="w", pady=(16, 0))

    def _tab_video(self):
        f = self._card("Vídeo y pantalla")
        self.ruta_vid = tk.StringVar(value="ningún archivo elegido")
        fila = ttk.Frame(f, style="Card.TFrame")
        fila.pack(fill="x")
        ttk.Button(fila, text="Elegir vídeo…",
                   command=self.elegir_video).pack(side="left")
        ttk.Label(fila, textvariable=self.ruta_vid, style="Card.TLabel",
                  width=40).pack(side="left", padx=10)

        b = ttk.Frame(f, style="Card.TFrame")
        b.pack(anchor="w", pady=(10, 0))
        ttk.Button(b, text="Reproducir vídeo", style="Accent.TButton",
                   command=self.reproducir_video).pack(side="left", padx=(0, 8))
        ttk.Button(b, text="Detener", command=self.detener).pack(side="left")

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=16)

        b2 = ttk.Frame(f, style="Card.TFrame")
        b2.pack(anchor="w")
        ttk.Button(b2, text="Seleccionar región…",
                   command=self.pick_region).pack(side="left", padx=(0, 10))
        self.lbl_region = ttk.Label(b2, text="pantalla completa", style="Card.TLabel")
        self.lbl_region.pack(side="left")
        b3 = ttk.Frame(f, style="Card.TFrame")
        b3.pack(anchor="w", pady=(10, 0))
        ttk.Button(b3, text="Espejar pantalla", style="Accent.TButton",
                   command=self.espejar).pack(side="left", padx=(0, 8))
        ttk.Button(b3, text="Detener", command=self.detener).pack(side="left")

        self.v_fps = tk.IntVar(value=4)
        fl = ttk.Frame(f, style="Card.TFrame")
        fl.pack(fill="x", pady=(16, 0))
        ttk.Label(fl, text="FPS máximo", style="Card.TLabel", width=12).pack(side="left")
        ttk.Scale(fl, from_=1, to=12, variable=self.v_fps, orient="horizontal",
                  command=lambda e: self.lbl_fps.config(
                      text=str(self.v_fps.get()))).pack(side="left", fill="x",
                                                        expand=True)
        self.lbl_fps = ttk.Label(fl, text="4", style="Card.TLabel", width=4)
        self.lbl_fps.pack(side="left", padx=6)
        self.v_loop = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Repetir vídeo en bucle",
                        variable=self.v_loop).pack(anchor="w", pady=6)
        ttk.Label(f, style="CardDim.TLabel", wraplength=430, justify="left",
                  text="Cada fotograma se envía como imagen completa (~0,3 s), así "
                       "que el techo real está en torno a 3-4 fps. Suficiente para "
                       "clips cortos y visualizadores, no para vídeo fluido."
                  ).pack(anchor="w", pady=(10, 0))

    def _tab_texto(self):
        f = self._card("Texto")
        self.v_texto = tk.StringVar(value="HOLA")
        fila = ttk.Frame(f, style="Card.TFrame")
        fila.pack(fill="x")
        ttk.Label(fila, text="Texto", style="Card.TLabel", width=11).pack(side="left")
        tk.Entry(fila, textvariable=self.v_texto, bg="#0a0c10", fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI", 11)).pack(side="left", fill="x",
                                             expand=True, ipady=5)

        self.color_texto = "ffffff"
        fc = ttk.Frame(f, style="Card.TFrame")
        fc.pack(fill="x", pady=12)
        ttk.Label(fc, text="Color", style="Card.TLabel", width=11).pack(side="left")
        ttk.Button(fc, text="Elegir…", command=self.elegir_color).pack(side="left")
        self.muestra = tk.Canvas(fc, width=32, height=24, bg="#ffffff",
                                 highlightthickness=0)
        self.muestra.pack(side="left", padx=8)

        fa = ttk.Frame(f, style="Card.TFrame")
        fa.pack(fill="x", pady=4)
        ttk.Label(fa, text="Animación", style="Card.TLabel", width=11).pack(side="left")
        self.cmb_anim = ttk.Combobox(fa, width=22, state="readonly",
                                     values=[a[0] for a in ANIMACIONES])
        self.cmb_anim.current(1)
        self.cmb_anim.pack(side="left")

        ff = ttk.Frame(f, style="Card.TFrame")
        ff.pack(fill="x", pady=4)
        ttk.Label(ff, text="Fuente", style="Card.TLabel", width=11).pack(side="left")
        self.cmb_font = ttk.Combobox(ff, width=22, state="readonly",
                                     values=["CUSONG", "SIMSUN", "VCR_OSD_MONO"])
        self.cmb_font.current(0)
        self.cmb_font.pack(side="left")

        self.v_vel = tk.IntVar(value=80)
        fv = ttk.Frame(f, style="Card.TFrame")
        fv.pack(fill="x", pady=4)
        ttk.Label(fv, text="Velocidad", style="Card.TLabel", width=11).pack(side="left")
        ttk.Scale(fv, from_=0, to=100, variable=self.v_vel, orient="horizontal",
                  command=lambda e: self.lbl_vel.config(
                      text=str(self.v_vel.get()))).pack(side="left", fill="x",
                                                        expand=True)
        self.lbl_vel = ttk.Label(fv, text="80", style="CardDim.TLabel", width=4)
        self.lbl_vel.pack(side="left")

        self.v_arco = tk.IntVar(value=0)
        fr = ttk.Frame(f, style="Card.TFrame")
        fr.pack(fill="x", pady=4)
        ttk.Label(fr, text="Arcoíris", style="Card.TLabel", width=11).pack(side="left")
        ttk.Scale(fr, from_=0, to=9, variable=self.v_arco, orient="horizontal",
                  command=lambda e: self.lbl_arco.config(
                      text=str(self.v_arco.get()))).pack(side="left", fill="x",
                                                         expand=True)
        self.lbl_arco = ttk.Label(fr, text="0 = off", style="CardDim.TLabel", width=7)
        self.lbl_arco.pack(side="left")

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=(14, 10))
        self.v_guardar_txt = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Guardar en el panel (sobrevive al reinicio)",
                        variable=self.v_guardar_txt).pack(anchor="w")
        ft = ttk.Frame(f, style="Card.TFrame")
        ft.pack(fill="x", pady=(6, 0))
        ttk.Label(ft, text="en el slot", style="Card.TLabel", width=11).pack(side="left")
        self.v_slot_txt = tk.IntVar(value=3)
        ttk.Combobox(ft, textvariable=self.v_slot_txt, width=6, state="readonly",
                     values=list(range(1, 11))).pack(side="left")

        ttk.Button(f, text="Mostrar texto", style="Accent.TButton",
                   command=self.enviar_texto).pack(anchor="w", pady=(16, 0))
        ttk.Label(f, style="CardDim.TLabel", wraplength=430, justify="left",
                  text="Esto usa el motor de texto del propio panel: lo desplaza él "
                       "solo, sin depender del PC."). pack(anchor="w", pady=(12, 0))

    def _tab_panel(self):
        f = self._card("Panel")
        fo = ttk.Frame(f, style="Card.TFrame")
        fo.pack(fill="x", pady=4)
        ttk.Label(fo, text="Orientación", style="Card.TLabel",
                  width=13).pack(side="left")
        self.cmb_ori = ttk.Combobox(fo, width=14, state="readonly",
                                    values=[o[0] for o in ORIENTACIONES])
        self.cmb_ori.current(0)
        self.cmb_ori.pack(side="left")
        ttk.Button(fo, text="Aplicar", command=self.aplicar_orientacion).pack(
            side="left", padx=8)

        fs = ttk.Frame(f, style="Card.TFrame")
        fs.pack(fill="x", pady=12)
        ttk.Label(fs, text="Slot", style="Card.TLabel", width=13).pack(side="left")
        self.v_slot = tk.IntVar(value=1)
        ttk.Combobox(fs, textvariable=self.v_slot, width=6, state="readonly",
                     values=list(range(1, 11))).pack(side="left")
        ttk.Button(fs, text="Mostrar", command=lambda: self.simple(
            "show_slot", self.v_slot.get())).pack(side="left", padx=6)
        ttk.Button(fs, text="Borrar", command=lambda: self.simple(
            "delete", self.v_slot.get())).pack(side="left")

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=14)
        ttk.Button(f, text="Modo reloj", command=lambda: self.simple(
            "set_clock_mode", 1)).pack(anchor="w", pady=3)
        ttk.Button(f, text="Borrar TODA la memoria del panel",
                   command=self.limpiar_rom).pack(anchor="w", pady=3)
        ttk.Label(f, style="CardDim.TLabel", wraplength=430, justify="left",
                  text="«Borrar toda la memoria» elimina las imágenes y GIFs "
                       "guardados en el panel, incluidos los que pusiste desde la "
                       "app del móvil. No se puede deshacer."
                  ).pack(anchor="w", pady=(8, 0))

    # ---------------- utilidades ----------------
    def log(self, m):
        def _():
            self.txt.configure(state="normal")
            self.txt.insert("end", time.strftime("%H:%M:%S") + "  " + str(m) + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")
        try:
            self.after(0, _)
        except Exception:
            pass

    def preview(self, img):
        self.current_img = img
        self._tk = ImageTk.PhotoImage(img.resize((320, 320), Image.NEAREST))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk)

    def filtro(self, src):
        """Remuestreo a usar segun el ajuste global."""
        f = FILTROS.get(self.v_escalado.get())
        return f if f is not None else filtro_auto(src, max(self.W, self.H))

    def set_fondo(self, rgb):
        self.fondo = tuple(int(v) for v in rgb)
        self.sw_fondo.configure(bg="#{:02x}{:02x}{:02x}".format(*self.fondo))
        self.refrescar_imagen()

    def elegir_fondo(self):
        c = colorchooser.askcolor(
            color="#{:02x}{:02x}{:02x}".format(*self.fondo),
            title="Color bajo las zonas transparentes")
        if c and c[0]:
            self.set_fondo(c[0])

    def _cambio_realce(self, _=None):
        self.lbl_nit.configure(text=str(self.v_nitidez.get()))
        self.refrescar_imagen()

    def _post(self, img):
        """Realce comun a imagen, GIF, video y pantalla."""
        c = self.v_colores.get()
        return realzar(img, self.v_nitidez.get(),
                       0 if c == "original" else int(c))

    # ---- indicador de actividad ----
    def _ini_tarea(self, texto):
        def _():
            self.lbl_tarea.configure(text=texto + "…")
            self.barra.configure(mode="indeterminate")
            self.barra.start(12)
        self.after(0, _)

    def _fin_tarea(self, texto="en reposo"):
        def _():
            self.barra.stop()
            self.barra.configure(value=0)
            self.lbl_tarea.configure(text=texto)
        self.after(0, _)

    def tarea(self, titulo, fn):
        """Ejecuta fn en segundo plano mostrando la barra de actividad."""
        def envoltorio():
            self._ini_tarea(titulo)
            try:
                fn()
                self._fin_tarea("listo · " + titulo)
            except Exception as ex:
                self._fin_tarea("ERROR: {}".format(ex)[:46])
                self.log("{} falló: {}: {}".format(titulo, type(ex).__name__, ex))
        threading.Thread(target=envoltorio, daemon=True).start()

    def _check(self):
        if self.dev is None:
            messagebox.showinfo("Pixel Matrix Studio", "Conéctate al panel primero.")
            return False
        return True

    def _lanzar(self, fn):
        self.detener()
        self.cancelar.clear()
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()

    def detener(self):
        self.cancelar.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=6.0)
        self.worker = None

    def simple(self, metodo, *args):
        """Ejecuta un comando del panel en segundo plano."""
        if not self._check():
            return

        def t():
            getattr(self.dev, metodo)(*args)
            self.log("{}({}) OK".format(metodo, ", ".join(map(str, args))))
        self.tarea(metodo, t)

    # ---------------- conexion ----------------
    def buscar(self):
        self.log("Buscando paneles…")
        self._ini_tarea("buscando paneles")

        def t():
            try:
                res = escanear()
            except Exception as ex:
                self.log("Error al buscar: {}".format(ex))
                self._fin_tarea("error al buscar")
                return
            self._fin_tarea("búsqueda terminada")
            self.dispositivos = res
            vals = ["{}  ·  {}  ({} dBm)".format(n, a, r) for n, a, r in res]
            self.after(0, lambda: self.cmb.configure(values=vals))
            if vals:
                self.after(0, lambda: self.cmb.current(0))
            self.log("{} panel(es) encontrado(s)".format(len(vals)))
        threading.Thread(target=t, daemon=True).start()

    def toggle_conexion(self):
        if self.dev is not None:
            self.detener()
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
            self._cm = self.dev = None
            self.btn_conn.configure(text="Conectar")
            self.lbl_estado.configure(text="  desconectado")
            self.lbl_info.configure(text="—")
            self.log("Desconectado")
            return
        i = self.cmb.current()
        if i < 0 or i >= len(self.dispositivos):
            messagebox.showinfo("Pixel Matrix Studio",
                                "Pulsa «Buscar» y elige tu panel primero.")
            return
        nombre, addr, _ = self.dispositivos[i]
        self.log("Conectando a {}…".format(nombre))
        self._ini_tarea("conectando")

        def t():
            try:
                cm = Client(addr)
                dev = cm.__enter__()
                info = dev.get_device_info()
                self._cm, self.dev = cm, dev
                self.W = getattr(info, "width", 64) or 64
                self.H = getattr(info, "height", 64) or 64
                txt = "tipo {} · {}×{}".format(
                    getattr(info, "device_type", "?"), self.W, self.H)
                self.after(0, lambda: self.btn_conn.configure(text="Desconectar"))
                self.after(0, lambda: self.lbl_estado.configure(
                    text="  conectado · " + nombre))
                self.after(0, lambda: self.lbl_info.configure(text=txt))
                self.after(0, lambda: self.lbl_prev.configure(
                    text="Vista previa  ·  {}×{}".format(self.W, self.H)))
                self._guardar_config(addr)
                self.log("Conectado. Panel " + txt)
                self._fin_tarea("conectado")
            except Exception as ex:
                self.log("No se pudo conectar: {}".format(ex))
                self._fin_tarea("no se pudo conectar")
                if "not found" in str(ex).lower():
                    self.log("Sugerencia: cierra la app del móvil — el panel solo "
                             "admite una conexión a la vez.")
        threading.Thread(target=t, daemon=True).start()

    def _brillo_hw(self, _=None):
        self.lbl_bhw.config(text=str(self.v_brillo_hw.get()))
        if self.dev is None:
            return
        if getattr(self, "_bh_job", None):
            self.after_cancel(self._bh_job)
        self._bh_job = self.after(
            350, lambda: self.simple("set_brightness", self.v_brillo_hw.get()))

    # ---------------- imagen ----------------
    def elegir_imagen(self):
        p = filedialog.askopenfilename(
            title="Elige una imagen",
            filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.bmp *.webp *.tiff"),
                       ("Todos", "*.*")])
        if p:
            self.ruta_img.set(os.path.basename(p))
            self._img = p
            self.fuente = ("imagen", p)
            self.refrescar_imagen()
            threading.Thread(target=self._avisar_resolucion, args=(p,),
                             daemon=True).start()

    def _cambio_pestana(self, _=None):
        """La vista previa sigue a la pestana activa, para no mostrar algo ajeno."""
        try:
            nombre = self.nb.tab(self.nb.select(), "text")
        except Exception:
            return
        ruta = {"Imagen": getattr(self, "_img", None),
                "GIF": getattr(self, "_gif", None)}.get(nombre)
        if ruta:
            self.fuente = ("imagen" if nombre == "Imagen" else "gif", ruta)
            self.refrescar_imagen()

    def _procesar(self, ruta, tipo):
        """Devuelve (imagen 64x64 lista, filtro usado) para imagen o GIF."""
        src = Image.open(ruta)
        filt = self.filtro(src)
        alfa = tiene_transparencia(src)
        self.after(0, lambda: self.lbl_alfa.configure(
            text="Este archivo tiene transparencia: el fondo elegido se ve debajo."
            if alfa else ""))
        img = fit_image(src, self.W, self.H, self.modo_fit.get(), filt, self.fondo)
        if tipo == "imagen":
            img = enhance(img, self.v_b.get(), self.v_c.get(), self.v_s.get())
        return self._post(img), filt

    def _avisar_resolucion(self, ruta):
        """Informa de cuanto detalle real trae el archivo frente al panel."""
        try:
            lw, lh = resolucion_logica(Image.open(ruta))
        except Exception:
            return
        if max(lw, lh) > max(self.W, self.H):
            self.log("Resolución lógica {}×{}: trae más detalle del que caben "
                     "en {}×{}, se perderá parte. Sube la nitidez para "
                     "compensar.".format(lw, lh, self.W, self.H))
        else:
            self.log("Resolución lógica {}×{}: cabe entera en el panel."
                     .format(lw, lh))

    def refrescar_imagen(self):
        """Repinta la vista previa de LO QUE ESTE CARGADO, sea imagen o GIF."""
        if self.fuente is None:
            return
        tipo, ruta = self.fuente
        try:
            img, filt = self._procesar(ruta, tipo)
            self.preview(img)
            self.lbl_prev.configure(text="Vista previa · {} · {}".format(
                "GIF" if tipo == "gif" else "imagen", NOMBRE_FILTRO.get(filt, "?")))
        except Exception as ex:
            self.preview(Image.new("RGB", (self.W, self.H), (40, 10, 10)))
            self.lbl_prev.configure(text="Vista previa · error")
            self.log("No se pudo generar la vista previa de {}: {}: {}".format(
                os.path.basename(ruta), type(ex).__name__, ex))

    def enviar_imagen(self):
        if not self._check():
            return
        p = getattr(self, "_img", None)
        if not p:
            messagebox.showinfo("Pixel Matrix Studio",
                                "Elige una imagen primero.")
            return
        # se reconstruye desde el archivo: nunca manda por error el fotograma
        # de un GIF que estuviera en la vista previa
        img, _ = self._procesar(p, "imagen")
        self.preview(img)
        datos = png_hex(img)
        slot = self.v_slot_img.get() if self.v_guardar_img.get() else 0

        def t():
            t0 = time.time()
            self.dev.send_image_hex(datos, ".png", save_slot=slot)
            self.log("Imagen enviada ({} B) en {:.2f} s".format(
                len(datos) // 2, time.time() - t0))
            if slot:
                self.dev.show_slot(slot)
                self.log("Guardada en el slot {} · se mantendrá al reiniciar"
                         .format(slot))
        self.tarea("enviando imagen", t)

    # ---------------- gif ----------------
    def elegir_gif(self):
        p = filedialog.askopenfilename(title="Elige un GIF",
                                       filetypes=[("GIF", "*.gif"), ("Todos", "*.*")])
        if p:
            self.ruta_gif.set(os.path.basename(p))
            self._gif = p
            self.fuente = ("gif", p)
            self.refrescar_imagen()
            threading.Thread(target=self._avisar_resolucion, args=(p,),
                             daemon=True).start()

    def enviar_gif(self):
        if not self._check():
            return
        p = getattr(self, "_gif", None)
        if not p:
            return
        slot = self.v_slot_gif.get() if self.v_guardar_gif.get() else 0
        modo = self.modo_fit.get()
        filt = self.filtro(Image.open(p))

        def t():
            self.log("Preparando GIF (escalado: {})…".format(
                NOMBRE_FILTRO.get(filt, "?")))
            c = self.v_colores.get()
            datos, n = preparar_gif(p, self.W, self.H, modo, filt,
                                    self.v_nitidez.get(),
                                    0 if c == "original" else int(c),
                                    self.fondo)
            self.log("{} fotogramas · {} B · subiendo…".format(n, len(datos)))
            t0 = time.time()
            self.dev.send_image_hex(datos.hex(), ".gif", save_slot=slot)
            self.log("GIF subido en {:.1f} s".format(time.time() - t0))
            if slot:
                self.dev.show_slot(slot)
                self.log("Guardado en el slot {} · se mantendrá al reiniciar"
                         .format(slot))
        self.tarea("subiendo GIF", t)

    # ---------------- video / pantalla ----------------
    def elegir_video(self):
        p = filedialog.askopenfilename(
            title="Elige un vídeo",
            filetypes=[("Vídeo", "*.mp4 *.avi *.mkv *.mov *.webm"), ("Todos", "*.*")])
        if p:
            self.ruta_vid.set(os.path.basename(p))
            self._vid = p

    def _enviar_frame(self, img):
        self.dev.send_image_hex(png_hex(img), ".png")
        self.after(0, lambda i=img: self.preview(i))

    def reproducir_video(self):
        if not self._check():
            return
        p = getattr(self, "_vid", None)
        if not p:
            return

        def t():
            import cv2
            cap = cv2.VideoCapture(p)
            if not cap.isOpened():
                self.log("No se pudo abrir el vídeo")
                return
            self.log("Reproduciendo… pulsa Detener para parar")
            n, t0, filt = 0, time.time(), None
            modo = self.modo_fit.get()
            while not self.cancelar.is_set():
                ok, frame = cap.read()
                if not ok:
                    if self.v_loop.get():
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    break
                ini = time.time()
                src = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                if filt is None:      # se decide una vez, no en cada fotograma
                    filt = self.filtro(src)
                    self.log("Escalado: {}".format(
                        NOMBRE_FILTRO.get(filt, "?")))
                img = self._post(enhance(
                    fit_image(src, self.W, self.H, modo, filt, self.fondo),
                    saturacion=1.25))
                try:
                    self._enviar_frame(img)
                except Exception as ex:
                    self.log("Corte: {}".format(ex))
                    break
                n += 1
                self.after(0, lambda v=n / max(0.001, time.time() - t0):
                           self.lbl_info.configure(text="{:.2f} fps".format(v)))
                resto = 1.0 / max(1, self.v_fps.get()) - (time.time() - ini)
                if resto > 0:
                    time.sleep(resto)
            cap.release()
            self.log("Vídeo detenido · {} fotogramas".format(n))
        self._lanzar(t)

    def pick_region(self):
        def hecho(r):
            self.region = r
            self.lbl_region.configure(text="{}×{} en ({},{})".format(
                r["width"], r["height"], r["left"], r["top"]))
        self.withdraw()
        self.after(220, lambda: RegionPicker(self, hecho))
        self.after(240, self.deiconify)

    def espejar(self):
        if not self._check():
            return

        def t():
            import mss
            self.log("Espejando… pulsa Detener para parar")
            n, t0, filt = 0, time.time(), None
            modo = self.modo_fit.get()
            with mss.mss() as sct:
                reg = self.region or sct.monitors[1]
                while not self.cancelar.is_set():
                    ini = time.time()
                    s = sct.grab(reg)
                    src = Image.frombytes("RGB", s.size, s.bgra, "raw", "BGRX")
                    if filt is None:
                        filt = self.filtro(src)
                        self.log("Escalado: {}".format(
                            NOMBRE_FILTRO.get(filt, "?")))
                    img = self._post(enhance(
                        fit_image(src, self.W, self.H, modo, filt, self.fondo),
                    saturacion=1.25))
                    try:
                        self._enviar_frame(img)
                    except Exception as ex:
                        self.log("Corte: {}".format(ex))
                        break
                    n += 1
                    self.after(0, lambda v=n / max(0.001, time.time() - t0):
                               self.lbl_info.configure(text="{:.2f} fps".format(v)))
                    resto = 1.0 / max(1, self.v_fps.get()) - (time.time() - ini)
                    if resto > 0:
                        time.sleep(resto)
            self.log("Espejo detenido · {} fotogramas".format(n))
        self._lanzar(t)

    # ---------------- texto ----------------
    def elegir_color(self):
        c = colorchooser.askcolor(color="#" + self.color_texto,
                                  title="Color del texto")
        if c and c[0]:
            self.color_texto = "{:02x}{:02x}{:02x}".format(*[int(v) for v in c[0]])
            self.muestra.configure(bg=c[1])

    def enviar_texto(self):
        if not self._check():
            return
        txt = self.v_texto.get().strip()
        if not txt:
            return
        anim = dict(ANIMACIONES)[self.cmb_anim.get()]
        slot = self.v_slot_txt.get() if self.v_guardar_txt.get() else 0
        args = dict(rainbow_mode=self.v_arco.get(), animation=anim,
                    speed=self.v_vel.get(), color=self.color_texto,
                    font=self.cmb_font.get(), save_slot=slot)

        def t():
            self.log("send_text({!r}, {})".format(txt, args))
            self.dev.send_text(txt, **args)
            self.log("Texto enviado")
            if slot:
                self.dev.show_slot(slot)
                self.log("Guardado en el slot {}".format(slot))
        self.tarea("enviando texto", t)

    # ---------------- panel ----------------
    def aplicar_orientacion(self):
        self.simple("set_orientation", dict(ORIENTACIONES)[self.cmb_ori.get()])

    def limpiar_rom(self):
        if not self._check():
            return
        if messagebox.askyesno(
                "Borrar memoria del panel",
                "Esto elimina TODAS las imágenes y GIFs guardados en el panel, "
                "incluidos los que pusiste desde la app del móvil.\n\n"
                "No se puede deshacer. ¿Continuar?"):
            self.simple("clear")

    # ---------------- config ----------------
    def _cargar_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                addr = json.load(f).get("address")
            if addr:
                self.dispositivos = [("último panel usado", addr, None)]
                self.cmb.configure(values=["último panel usado  ·  " + addr])
                self.cmb.current(0)
                self.log("Panel recordado: " + addr + " — pulsa Conectar")
        except Exception:
            pass

    def _guardar_config(self, addr):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"address": addr}, f)
        except Exception:
            pass

    def _cerrar(self):
        self.detener()
        try:
            if self._cm is not None:
                self._cm.__exit__(None, None, None)
        except Exception:
            pass
        self.destroy()
        os._exit(0)


if __name__ == "__main__":
    if Client is None:
        print("Falta pypixelcolor:  pip install pypixelcolor")
        raise SystemExit(1)
    App().mainloop()
