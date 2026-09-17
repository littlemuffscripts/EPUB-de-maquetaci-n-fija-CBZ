#!/usr/bin/env python3
"""EPUB de maquetación fija → CBZ, conservando imágenes, texto y fuentes."""
from __future__ import annotations

import argparse
import functools
import math
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET


class ConversionError(Exception):
    pass


@dataclass(frozen=True)
class Page:
    number: int
    path: str
    width: int
    height: int


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def read_xml(path):
    try:
        return ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ConversionError(f"No se puede leer {path.name}: {exc}") from exc


def extract_epub(source, root):
    """Extract regular files under root; never follow ZIP symlinks."""
    try:
        with zipfile.ZipFile(source) as archive:
            seen = set()
            for entry in archive.infolist():
                name = entry.filename
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith(("/", "\\")) or "\\" in name
                        or ".." in parts or ":" in parts[0]
                        or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ConversionError(f"Ruta no segura dentro del EPUB: {name!r}")
                target = root.joinpath(*parts)
                if not target.resolve().is_relative_to(root.resolve()):
                    raise ConversionError(f"Ruta fuera del EPUB: {name!r}")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                key = str(target).casefold()
                if key in seen:
                    raise ConversionError(f"Ruta duplicada dentro del EPUB: {name!r}")
                seen.add(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ConversionError(f"No se puede extraer el EPUB: {exc}") from exc
    encryption = root / "META-INF/encryption.xml"
    if encryption.exists() and any(
        local_name(e.tag) == "EncryptedData" for e in read_xml(encryption).iter()
    ):
        raise ConversionError(
            "El EPUB declara recursos cifrados u ofuscados, posiblemente fuentes. "
            "Este script no los descifra; necesita recursos legibles."
        )


def local_reference(base, href):
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise ConversionError(f"Referencia no local: {href!r}")
    decoded = unquote(parsed.path)
    if "\\" in decoded or decoded.startswith("/"):
        raise ConversionError(f"Referencia no segura: {href!r}")
    path = posixpath.normpath(posixpath.join(base, decoded))
    if path == ".." or path.startswith("../"):
        raise ConversionError(f"Referencia fuera del EPUB: {href!r}")
    return path


def positive_pixels(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", value)
    if match:
        number = float(match[1])
        if 0 < number <= 20000:
            return math.ceil(number)
    return None


def page_dimensions(document, fallback):
    for element in document.iter():
        if local_name(element.tag) == "meta" and element.get("name", "").lower() == "viewport":
            content = element.get("content", "")
            width = re.search(r"(?:^|[,;\s])width\s*=\s*([\d.]+)", content, re.I)
            height = re.search(r"(?:^|[,;\s])height\s*=\s*([\d.]+)", content, re.I)
            if width and height:
                w, h = positive_pixels(width[1]), positive_pixels(height[1])
                if w and h:
                    return w, h
    if local_name(document.tag) == "svg":
        w, h = positive_pixels(document.get("width", "")), positive_pixels(document.get("height", ""))
        if w and h:
            return w, h
        box = document.get("viewBox", "").replace(",", " ").split()
        if len(box) == 4:
            w, h = positive_pixels(box[2]), positive_pixels(box[3])
            if w and h:
                return w, h
    if fallback:
        return fallback
    raise ConversionError("Falta el tamaño fijo de página (viewport o original-resolution).")


def load_pages(root):
    container = read_xml(root / "META-INF/container.xml")
    roots = [e for e in container.iter() if local_name(e.tag) == "rootfile"]
    if not roots:
        raise ConversionError("El EPUB no contiene un paquete OPF.")
    opf_path = local_reference("", roots[0].get("full-path", ""))
    package = read_xml(root / opf_path)
    base, fixed, fallback = posixpath.dirname(opf_path), False, None
    for element in package.iter():
        if local_name(element.tag) != "meta":
            continue
        if element.get("property") == "rendition:layout":
            fixed = (element.text or "").strip() == "pre-paginated"
        if element.get("name") == "fixed-layout" and element.get("content") == "true":
            fixed = True
        if element.get("name") == "original-resolution":
            match = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", element.get("content", ""))
            if match:
                w, h = positive_pixels(match[1]), positive_pixels(match[2])
                if w and h:
                    fallback = (w, h)
    manifest = {e.get("id"): e for e in package.iter() if local_name(e.tag) == "item"}
    spine = next((e for e in package if local_name(e.tag) == "spine"), None)
    if spine is None:
        raise ConversionError("El EPUB no tiene orden de lectura (spine).")
    pages = []
    for itemref in spine:
        if local_name(itemref.tag) != "itemref" or itemref.get("linear") == "no":
            continue
        item = manifest.get(itemref.get("idref"))
        if item is None:
            raise ConversionError(f"Falta el recurso del spine: {itemref.get('idref')}")
        properties = set(itemref.get("properties", "").split())
        if (not (fixed or "rendition:layout-pre-paginated" in properties)
                or "rendition:layout-reflowable" in properties):
            raise ConversionError(
                "El EPUB es adaptable (reflowable) o no declara maquetación fija. "
                "Este script solo convierte cómics de maquetación fija."
            )
        if item.get("media-type") not in ("application/xhtml+xml", "image/svg+xml", "text/html"):
            raise ConversionError(f"Tipo de página no compatible: {item.get('media-type')}")
        path = local_reference(base, item.get("href", ""))
        try:
            width, height = page_dimensions(read_xml(root / path), fallback)
        except ConversionError as exc:
            raise ConversionError(f"{path}: {exc}") from exc
        pages.append(Page(len(pages) + 1, path, width, height))
    if not pages:
        raise ConversionError("El EPUB no contiene páginas de lectura.")
    return pages


def select_pages(pages, selection):
    if not selection:
        return pages
    selected = set()
    for part in selection.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise ConversionError("--paginas debe ser, por ejemplo, 10-11 o 1,10-11,20.")
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last < first or last > len(pages):
            raise ConversionError(f"Rango {part!r} inválido: hay {len(pages)} páginas.")
        selected.update(range(first, last + 1))
    return [page for page in pages if page.number in selected]


class LocalHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        # Preserve CSS and fonts, but never run scripts included in a book.
        self.send_header("Content-Security-Policy", "script-src 'none'; connect-src 'none'")
        super().end_headers()


@contextmanager
def local_server(root):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(LocalHandler, directory=str(root))
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def duration(seconds):
    seconds = max(0, round(seconds))
    return f"{seconds // 60} min {seconds % 60:02d} s"


def record_failed_request(request, problems):
    # The server deliberately disables book JavaScript. Chromium reports those
    # expected blocks as failed requests too; they are not missing page assets.
    failure = request.failure or ""
    if request.resource_type == "script" and failure.lower() in (
        "csp", "net::err_blocked_by_csp"
    ):
        return
    problems.append(f"{request.resource_type}: {request.url} ({failure})")


def convert(source, destination, browser, args):
    if destination.exists() and not args.sobrescribir:
        raise ConversionError(f"Ya existe {destination}. Usa --sobrescribir para sustituirlo.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="epub-cbz-") as temp:
        root = Path(temp) / "epub"
        root.mkdir()
        extract_epub(source, root)
        all_pages = load_pages(root)
        pages = select_pages(all_pages, args.paginas)
        print(f"\n{source.name}: {len(all_pages)} páginas; convertir {len(pages)} "
              f"a escala {args.escala:g}.", flush=True)
        fd, staged_name = tempfile.mkstemp(
            prefix=f".{destination.stem}-", suffix=".cbz.tmp", dir=destination.parent
        )
        os.close(fd)
        staged = Path(staged_name)
        try:
            with local_server(root) as origin:
                context = browser.new_context(
                    device_scale_factor=args.escala, service_workers="block",
                    viewport={"width": pages[0].width, "height": pages[0].height}
                )
                try:
                    def route_request(route):
                        url = route.request.url
                        if url.startswith(origin + "/") or url.startswith(("data:", "blob:")):
                            route.continue_()
                        else:
                            route.abort("blockedbyclient")
                    context.route("**/*", route_request)
                    page = context.new_page()
                    page.set_default_timeout(60000)
                    problems = []
                    page.on("requestfailed", lambda request: record_failed_request(request, problems))
                    page.on("response", lambda response: problems.append(
                        f"HTTP {response.status}: {response.url}"
                    ) if response.status >= 400 else None)
                    page.on("console", lambda message: problems.append(message.text)
                            if any(term in message.text.lower() for term in
                                   ("failed to decode downloaded font", "ots parsing error")) else None)
                    with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_STORED) as cbz:
                        for index, item in enumerate(pages, 1):
                            problems.clear()
                            print(f"  [{index}/{len(pages)}] Página {item.number}: "
                                  "cargando imágenes y texto…", flush=True)
                            page.set_viewport_size({"width": item.width, "height": item.height})
                            page.goto(origin + "/" + quote(item.path, safe="/"), wait_until="load")
                            status = page.evaluate("""async () => {
                                await Promise.race([
                                    document.fonts.ready,
                                    new Promise((_, reject) => setTimeout(
                                        () => reject(new Error("Las fuentes no terminan de cargar")), 60000))
                                ]);
                                const brokenImages = [];
                                for (const img of document.querySelectorAll("img")) {
                                    try { await img.decode(); } catch (_) {
                                        brokenImages.push(img.currentSrc || img.src);
                                    }
                                    if (!img.naturalWidth && !brokenImages.includes(img.src))
                                        brokenImages.push(img.src);
                                }
                                const failedFonts = Array.from(document.fonts)
                                    .filter(font => font.status === "error").map(font => font.family);
                                await new Promise(resolve =>
                                    requestAnimationFrame(() => requestAnimationFrame(resolve)));
                                return {brokenImages, failedFonts};
                            }""")
                            problems.extend(f"Imagen no cargada: {url}" for url in status["brokenImages"])
                            problems.extend(f"Fuente no cargada: {name}" for name in status["failedFonts"])
                            if problems:
                                raise ConversionError(
                                    f"Página {item.number}: faltan recursos; se cancela para "
                                    "evitar páginas incompletas.\n  " + "\n  ".join(problems[:8])
                                )
                            png = page.screenshot(
                                type="png", full_page=False, animations="disabled",
                                clip={"x": 0, "y": 0, "width": item.width, "height": item.height}
                            )
                            cbz.writestr(f"pagina-{item.number:06d}.png", png)
                            elapsed = time.monotonic() - started
                            eta = elapsed / index * (len(pages) - index)
                            print(f"  [{index}/{len(pages)}] Página {item.number} guardada · "
                                  f"transcurrido {duration(elapsed)} · "
                                  f"faltan aprox. {duration(eta)}", flush=True)
                finally:
                    context.close()
            if args.sobrescribir:
                os.replace(staged, destination)
            else:
                # Publish atomically without overwriting a concurrently created file.
                os.link(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    print(f"Listo: {destination}\n{len(pages)} páginas · "
          f"{destination.stat().st_size / 1024**2:.1f} MB · "
          f"{duration(time.monotonic() - started)}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convierte cómics EPUB de maquetación fija a CBZ con texto y fuentes.",
        epilog="Requiere Playwright y Chromium; no necesita Calibre ni Poppler."
    )
    parser.add_argument("entrada", type=Path, help="EPUB o carpeta (sin subcarpetas).")
    parser.add_argument("-o", "--salida", type=Path, help="CBZ de salida o carpeta de destino.")
    parser.add_argument("--escala", type=float, default=2, help="Resolución, de 0.25 a 4 (por defecto 2).")
    parser.add_argument("--paginas", help="Prueba parcial: 10-11 o 1,10-11,20; incluye portada.")
    parser.add_argument("--sobrescribir", action="store_true", help="Sustituye un CBZ existente.")
    parser.add_argument("--browser", choices=("chromium", "chrome"), default="chromium",
                        help="Navegador: Chromium de Playwright o Google Chrome instalado.")
    args = parser.parse_args(argv)
    if not math.isfinite(args.escala) or not 0.25 <= args.escala <= 4:
        parser.error("--escala debe estar entre 0.25 y 4.")
    source = args.entrada.expanduser().resolve()
    batch = source.is_dir()
    if batch:
        sources = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() == ".epub")
        if not sources:
            parser.error("La carpeta no contiene archivos EPUB.")
    elif source.is_file() and source.suffix.lower() == ".epub":
        sources = [source]
    else:
        parser.error("La entrada debe ser un .epub existente o una carpeta.")
    output = args.salida.expanduser().resolve() if args.salida else None
    if batch and output and output.suffix.lower() == ".cbz":
        parser.error("Para convertir una carpeta, -o debe indicar una carpeta.")
    if not batch and output and not output.is_dir() and output.suffix.lower() not in (".cbz", ""):
        parser.error("El archivo de salida debe terminar en .cbz.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Falta Playwright. Instálalo en un entorno virtual:\n"
              "  python3 -m venv .venv\n  source .venv/bin/activate\n"
              "  python -m pip install playwright\n  python -m playwright install chromium",
              file=sys.stderr)
        return 1
    failures = 0
    try:
        with sync_playwright() as playwright:
            options = {"channel": "chrome"} if args.browser == "chrome" else {}
            browser = playwright.chromium.launch(headless=True, **options)
            try:
                for item in sources:
                    if output is None:
                        destination = item.with_suffix(".cbz")
                    elif not batch and output.suffix.lower() == ".cbz" and not output.is_dir():
                        destination = output
                    else:
                        destination = output / item.with_suffix(".cbz").name
                    try:
                        convert(item, destination, browser, args)
                    except Exception as exc:
                        failures += 1
                        print(f"\nERROR ({item.name}): {exc}", file=sys.stderr, flush=True)
            finally:
                browser.close()
    except KeyboardInterrupt:
        print("\nInterrumpido. No se ha guardado un CBZ incompleto.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"No se pudo iniciar o mantener el navegador: {exc}\n"
              "Para instalar Chromium: python -m playwright install chromium\n"
              "Si tienes Google Chrome instalado, prueba --browser chrome.", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
