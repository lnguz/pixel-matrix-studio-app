# Pixel Matrix Studio

Aplicación de escritorio para Windows que controla por **Bluetooth LE** los paneles
LED de píxeles compatibles con **iPixel Color** (dispositivos que se anuncian como
`LED_BLE_*`), sin necesidad de la aplicación móvil.

Las dimensiones se leen del propio dispositivo, por lo que se adapta a los distintos
tamaños de panel. Verificada sobre un panel de 64×64 (`device type 128`).

---

## Funciones

| Pestaña | Descripción |
|---|---|
| **Imagen** | Envío de PNG, JPG, WEBP y BMP con vista previa y ajustes de brillo, contraste y saturación. |
| **GIF** | Sube la animación completa. El panel la reproduce en bucle de forma autónoma, sin el PC conectado. |
| **Vídeo y pantalla** | Reproducción de MP4, AVI, MKV, MOV y WEBM fotograma a fotograma, o espejo de una región del escritorio. |
| **Texto** | Motor de texto del propio panel: animación, arcoíris, velocidad, color y tres tipografías. |
| **Panel** | Brillo por hardware, encendido, orientación, slots de memoria, modo reloj y borrado. |

### Ajustes de imagen

Se aplican por igual a imagen, GIF, vídeo y espejo de pantalla:

- **Escalado** — `Automático`, `Pixel art nativo` (nearest), `Pixel art grande` (área) y `Foto` (Lanczos).
- **Encaje** — cubrir, ajustar o estirar.
- **Nitidez** — máscara de enfoque para recuperar definición tras reducir.
- **Colores** — reducción de paleta; mejora la legibilidad en un panel LED.
- **Fondo** — color aplicado bajo las zonas transparentes de PNG y GIF.

### Memoria del panel

El dispositivo dispone de **10 slots**. El contenido guardado en un slot persiste
tras apagar y encender; sin slot, se pierde al cortar la alimentación.

---

## Instalación

### Ejecutable

Descargar `PixelMatrixStudio.exe` desde *Releases*. No requiere Python ni instalación.

### Desde el código fuente

```powershell
git clone https://github.com/lnguz/pixel-matrix-studio-app.git
cd pixel-matrix-studio-app
python -m pip install -r requirements.txt
python pixel_matrix_studio.py
```

### Compilación del ejecutable

```powershell
.\build.ps1
```

---

## Requisitos

- Windows 10 u 11. Python 3.10 o superior únicamente para ejecutar desde el código.
- **Adaptador Bluetooth 4.0 o superior.** Si el equipo no dispone de Bluetooth
  integrado, es suficiente un dongle USB.
- El panel **admite una única conexión simultánea**: la aplicación móvil debe estar
  cerrada antes de conectar desde el PC.

---

## Protocolo

La comunicación se apoya en [`pypixelcolor`](https://pypi.org/project/pypixelcolor/).
Referencia técnica de los puntos relevantes:

- La escritura se realiza sobre la característica `0000fa02-…` y las respuestas
  llegan por `0000fa03-…`. El servicio `0000ae00-…` corresponde a OTA de Telink y
  no interviene en el dibujado.
- Las imágenes se transmiten en **ventanas de 12 KB** con cabecera
  `[LEN(2)][02 00 opción][TAMAÑO(4 LE)][CRC32(4 LE)][00 slot]`, y **cada ventana
  requiere confirmación por ACK**. Una trama sin CRC o que no espere el ACK es
  aceptada por el dispositivo pero produce ruido aleatorio en pantalla y deja el
  panel bloqueado hasta cortarle la alimentación.
- El dibujo píxel a píxel (`0A 00 05 01 00 R G B X Y`) es funcional, pero el panel
  **ejecuta únicamente el primer comando de cada paquete BLE**, por lo que agrupar
  varios no aporta nada. Un fotograma completo exige 4096 escrituras, alrededor de
  5,4 segundos.
- La consulta `[08 00 01 80 h m s 00]` devuelve el tipo de dispositivo, del que se
  derivan sus dimensiones.

### Tratamiento de imagen

- **El escalado de pixel art requiere dos estrategias distintas.** Con arte cercano
  a la resolución final (≤128 px), *nearest* preserva el píxel definido. Con arte
  renderizado a gran tamaño (400 px, 1500 px, 2560 px), *nearest* muestrea uno de
  cada 6 a 40 píxeles y degrada la imagen: en ese caso corresponde promediar el área.
- **La paleta de un GIF se calcula una sola vez para toda la animación.** Cuantizar
  fotograma a fotograma hace que cada cuadro seleccione colores ligeramente distintos
  y las zonas estáticas parpadeen. Medición sobre una animación de referencia: 665
  píxeles modificados por cuadro frente a los 428 del original; componiendo todos los
  fotogramas en un montaje único y cuantizando de una vez, la cifra baja a 425.
- **`disposal=1` en lugar de `2`.** Restaurar al color de fondo entre fotogramas
  produce destellos negros.

---

## Dependencias

- [`pypixelcolor`](https://pypi.org/project/pypixelcolor/) — implementación del protocolo iPIXEL.
- [`bleak`](https://github.com/hbldh/bleak) — Bluetooth LE multiplataforma.
- [`Pillow`](https://python-pillow.org/) — procesado de imagen.
- [`opencv-python`](https://pypi.org/project/opencv-python/) — decodificación de vídeo.
- [`mss`](https://pypi.org/project/mss/) — captura de pantalla.

Documentación del protocolo consultada:
[`ha-ipixel-color`](https://github.com/cagcoach/ha-ipixel-color).

---

## Aviso

Proyecto independiente, sin relación con Divoom ni con los desarrolladores de la
aplicación iPixel Color.
