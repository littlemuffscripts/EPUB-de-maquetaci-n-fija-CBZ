# EPUB de cómic → CBZ con texto

Renderiza las páginas originales con Chromium y conserva imágenes,
texto, fuentes y posiciones. 

Solo admite EPUB de maquetación fija. Rechaza libros adaptables y recursos cifrados
u ofuscados (incluidas fuentes), en vez de generar páginas incompletas. Necesita
Python 3.9 o posterior y no modifica el EPUB original.

## Instalación en Unix

Guarda el script en una carpeta, entra en ella desde Terminal con `cd` y ejecuta:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install playwright
python -m playwright install chromium
```

La instalación necesita Internet; la conversión se realiza localmente.
En futuras sesiones vuelve a esa carpeta y ejecuta `source .venv/bin/activate`.

## Prueba rápida de dos páginas

```bash
python epub_a_cbz.py "/ruta/comic.epub" --paginas 10-11 -o "/ruta/prueba.cbz"
```

La numeración sigue el orden de lectura e incluye la portada como página 1.
También admite `--paginas 1,10-11,20`. Conserva los números originales en el CBZ.

## Convertir el cómic completo

```bash
python epub_a_cbz.py "/ruta/comic.epub" -o "/ruta/comic-con-texto.cbz"
```

Sin `-o`, deja el CBZ junto al EPUB. Para sustituir un CBZ existente añade
`--sobrescribir`. Para convertir una carpeta completa, sin recorrer subcarpetas:

```bash
python epub_a_cbz.py "/ruta/comics" -o "/ruta/convertidos"
```

Si falla un cómic, informa del error y continúa con los demás.

## Calidad y progreso

La escala predeterminada es 2: una página de 964 × 1360 se guarda a 1928 × 2720
píxeles. El texto se renderiza a esa resolución. Los dibujos no ganan detalle que
no exista en el EPUB. Añade `--escala 1` para reducir tamaño y tiempo.

El script muestra la página que carga, confirma cada página guardada e indica
tiempo transcurrido y restante estimado. Si falla una imagen o fuente, cancela ese
cómic e informa del recurso. La carga tiene un límite de aproximadamente un minuto;
una conversión completa de muchas páginas puede tardar varios minutos.

Solo guarda el CBZ definitivo cuando todas las páginas solicitadas terminan.
Si algo falla, conserva el CBZ anterior. Puedes interrumpir con **Ctrl+C**.

Si ya tienes Google Chrome instalado, puedes usar `--browser chrome` y omitir
la descarga de Chromium; el paquete Python `playwright` sigue siendo necesario.

El navegador recibe temporalmente los archivos mediante un servidor en
`127.0.0.1`. El script bloquea recursos externos y scripts incluidos en el EPUB.
Los avisos CSP de esos scripts bloqueados se ignoran, ya que el bloqueo es
intencionado. Los fallos de imágenes, hojas de estilo y fuentes siguen deteniendo
la conversión. Los elementos interactivos que requieren JavaScript no se ejecutan.
Los temporales se eliminan al acabar. Conviene revisar primero dos páginas en tu
lector: la compatibilidad del CSS y las fuentes puede variar según el libro.

## Validación de esta entrega

Se ha comprobado el funcionamiento con más de 10 cómics distintos con buenos resultados. 


Referencias de la implementación: [Playwright para Python](https://playwright.dev/python/docs/library)
y [capturas de páginas](https://playwright.dev/python/docs/screenshots).
