"""
scraper_samp_json.py
----------------------
Para extraer los planes de estudio del SAMP de las carreras de IDM y sus avenidas 


USO:
    python scraper_samp_json.py
"""

import json
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------

PROGRAM_CLAVES = ["IDM19", "IDM26", "ICI19", "ICT26"]

BASE_PROGRAM_URL = "https://samp.itesm.mx/Programas/VistaPrograma"
BASE_MATERIA_URL = "https://samp.itesm.mx/Materias/VistaPreliminarMateria"

OUTPUT_DIR = Path("planes_json")
DELAY_SECONDS = 2.0
DELAY_SECONDS_MATERIA = 1 
NAV_TIMEOUT_MS = 30000

# Cache global clave_materia -> detalle, para no volver a pedir la misma
# materia dos veces si se repite entre programas 
_MATERIA_CACHE = {}

PLAN_RE = re.compile(r"Plan\s+(\d{4})", re.IGNORECASE)
CIP_RE = re.compile(r"(\d{4,6})\s*$")

# Una celda de periodo está "vacía" (la materia NO corre esa semana) solo
# si su clase es EXACTAMENTE "FlechaVaciaDIVPeriodoES<n>".
VACIA_RE = re.compile(r"^FlechaVaciaDIVPeriodoES\d+$")


def build_program_url(clave: str) -> str:
    return f"{BASE_PROGRAM_URL}?clave={clave}&modoVista=Default&idioma=ES&cols=0"


def build_materia_url(clave: str, lang: str = "ES") -> str:
    return f"{BASE_MATERIA_URL}?clave={clave}&lang={lang}"


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")


def expand_full_view(page):
    labels = ["Vista por periodo", "Mostrar Requisitos", "Mostrar Competencias"]
    for label in labels:
        try:
            locator = page.get_by_text(label, exact=False).first
            if locator.is_visible(timeout=1500):
                locator.click(timeout=1500)
                page.wait_for_timeout(500)
        except Exception:
            pass


def wait_for_curricular_map(page):
    try:
        page.wait_for_selector("img[src*='ajax-loader']", state="hidden", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


def extract_program_name_and_plan(soup):
    """El nombre del programa y el año del plan viven en dos <span class="translate">
    consecutivos """
    nombre_programa = None
    plan = None

    for span in soup.select("span.translate[data-es]"):
        m = PLAN_RE.search(span.get("data-es", ""))
        if m:
            # El span del plan vive en un <div class="titulo3">; el
            # nombre del programa está en el <div class="titulo2">
            # justo antes (son divs hermanos, no spans hermanos).
            plan_div = span.find_parent("div")
            if plan_div:
                prev_div = plan_div.find_previous_sibling("div")
                if prev_div:
                    name_span = prev_div.find("span", class_="translate")
                    if name_span and name_span.get_text(strip=True):
                        plan = int(m.group(1))
                        nombre_programa = name_span.get_text(strip=True)
            break

    return nombre_programa, plan


def extract_cip_programa(soup):
    """Código CIP del PROGRAMA. Vive en:
    """
    tag = soup.select_one("#CIPCode")
    if not tag:
        return None
    texto = tag.get("data-es") or tag.get_text(strip=True)
    m = CIP_RE.search(texto or "")
    return m.group(1) if m else None


def parse_plan_html(html: str, clave: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    nombre_programa, plan = extract_program_name_and_plan(soup)
    cip_programa = extract_cip_programa(soup)

    semestres = []

    for periodo_table in soup.select("table.DIVPeriodoES"):
        header_span = periodo_table.select_one("div.notaPeriodo span")
        if not header_span:
            continue
        nombre_semestre = header_span.get_text(strip=True).replace("\xa0", " ").strip()

        # Etiquetas de columnas de semana, en orden de aparición, sin duplicados
        semana_labels = []
        for span in periodo_table.select("span.translate[data-es]"):
            label = span.get("data-es")
            if label and label.startswith("Semana") and label not in semana_labels:
                semana_labels.append(label)

        unidades = []
        for row in periodo_table.select("tr.ClaveDescripcionod"):
            clave_link = row.select_one("a.vpmateriapuente[data-language='ES']")
            if not clave_link:
                continue  # fila en inglés (el HTML trae ambos idiomas) -- se ignora
            clave_materia = clave_link.get_text(strip=True)

            tds = row.find_all("td", recursive=False)
            descripcion = tds[1].get_text(" ", strip=True) if len(tds) > 1 else ""

            # Bloques de periodo (uno por columna "Semana X-Y")
            semanas_activas = []
            for idx, lt in enumerate(row.select("table.Lineal")):
                divs = [d for d in lt.select("td > div") if d.get("class")]
                activo = any(not VACIA_RE.match(d["class"][0]) for d in divs)
                if activo and idx < len(semana_labels):
                    semanas_activas.append(semana_labels[idx])

            # Columnas numéricas (CL, L, A, CA, UDC -- "A" no existe en
            # todos los planes, se incluye solo si la celda existe)
            valores = {}
            for campo in ["CL", "L", "A", "CA", "UDC"]:
                celda = row.select_one(f"td.{campo}")
                if celda:
                    span = celda.select_one("span")
                    texto = (span.get_text(strip=True) if span else celda.get_text(strip=True))
                    if texto.isdigit():
                        valores[campo] = int(texto)

            requisitos = None
            req_td = row.select_one("td[name='Requisitos']")
            if req_td:
                texto = req_td.get_text(" ", strip=True)
                requisitos = texto if texto else None

            # Equivalencias: mismo patrón que Requisitos/Competencias 
            equivalencias = []
            eq_td = row.select_one("td[name='Equivalencias']")
            if eq_td:
                for a in eq_td.select("a.RefMateria"):
                    c = (a.get("data-clave") or a.get_text(strip=True)).strip()
                    if c:
                        equivalencias.append(c)

            competencias = []
            comp_td = row.select_one("td[name='Competencias']")
            if comp_td:
                for a in comp_td.select("a.refCompetencia"):
                    c = a.get_text(strip=True)
                    if c:
                        competencias.append(c)

            unidades.append(
                {
                    "clave": clave_materia,
                    "descripcion": descripcion,
                    **valores,
                    "semanas_activas": semanas_activas,
                    "requisitos": requisitos,
                    "equivalencias": equivalencias,
                    "competencias": competencias,
                }
            )

        semestres.append({"nombre": nombre_semestre, "unidades_formacion": unidades})

    return {
        "clave_programa": clave,
        "nombre_programa": nombre_programa,
        "plan": plan,
        "cip_programa": cip_programa,
        "semestres": semestres,
    }


def parse_materia_html(html: str, clave: str) -> dict:
    """Parsea /Materias/VistaPreliminarMateria?clave=X&lang=ES.
      1) El CIP propio de la materia:
         seguido del nombre de la disciplina como texto suelto 
    """
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one("#sintetico_ES") or soup

    cip_tag = panel.select_one("a.CIP[data-cip]")
    cip_codigo = cip_tag.get("data-cip").strip() if cip_tag and cip_tag.get("data-cip") else None

    cip_disciplina = None
    if cip_tag:
        nxt = cip_tag.next_sibling
        texto = (nxt or "").replace("\xa0", " ").strip()
        cip_disciplina = texto or None

    return {
        "clave": clave,
        "cip_codigo": cip_codigo,
        "cip_disciplina": cip_disciplina,
    }


def fetch_materia_cip(page, clave: str) -> dict:
    """Trae y parsea la página de detalle de una materia para sacar su CIP
    propio. Usa un cache global para no repetir la petición si la misma
    materia ya se consultó)."""
    if clave in _MATERIA_CACHE:
        return _MATERIA_CACHE[clave]

    url = build_materia_url(clave)
    try:
        page.goto(url, wait_until="load")
        page.wait_for_selector("#sintetico_ES", timeout=10000)
    except Exception as e:
        print(f"    [!] No se pudo cargar detalle de materia {clave}: {e}")
        data = {"clave": clave, "cip_codigo": None, "cip_disciplina": None}
        _MATERIA_CACHE[clave] = data
        return data

    html = page.content()
    data = parse_materia_html(html, clave)
    _MATERIA_CACHE[clave] = data
    time.sleep(DELAY_SECONDS_MATERIA)
    return data


def enrich_with_cip_por_materia(page, data: dict) -> None:
    """Recorre todas las unidades de formación del programa ya parseado y les
    agrega 'cip_codigo' / 'cip_disciplina', visitando la página de detalle de
    cada materia única con cache para no repetir materias ya vistas."""
    for semestre in data["semestres"]:
        for unidad in semestre["unidades_formacion"]:
            detalle = fetch_materia_cip(page, unidad["clave"])
            unidad["cip_codigo"] = detalle["cip_codigo"]
            unidad["cip_disciplina"] = detalle["cip_disciplina"]


def scrape_program_json(browser, clave: str) -> dict:
    print(f"\n=== Programa: {clave} ===")
    context = browser.new_context()
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

    url = build_program_url(clave)
    page.goto(url, wait_until="load")
    wait_for_curricular_map(page)
    expand_full_view(page)
    wait_for_curricular_map(page)

    html = page.content()

    debug_dir = OUTPUT_DIR / "_debug_html"
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{safe_filename(clave)}.html").write_text(html, encoding="utf-8")

    data = parse_plan_html(html, clave)

    n_materias = sum(len(s["unidades_formacion"]) for s in data["semestres"])
    n_equiv = sum(
        1
        for s in data["semestres"]
        for u in s["unidades_formacion"]
        if u["equivalencias"]
    )
    print(f"  Semestres detectados: {len(data['semestres'])}")
    print(f"  Materias detectadas: {n_materias}")
    print(f"  Materias con equivalencias: {n_equiv}")
    print(f"  CIP de programa: {data['cip_programa']}")
    if n_materias == 0:
        print("  [!] No se detectó ninguna materia -- revisa el .html de debug.")

    # Visita la página de detalle de cada materia única para sacar su CIP
    materias_unicas = {u["clave"] for s in data["semestres"] for u in s["unidades_formacion"]}
    print(f"  Consultando CIP de {len(materias_unicas)} materias (con cache global)...")
    enrich_with_cip_por_materia(page, data)
    n_con_cip = sum(
        1
        for s in data["semestres"]
        for u in s["unidades_formacion"]
        if u.get("cip_codigo")
    )
    print(f"  Materias con CIP propio detectado: {n_con_cip}/{n_materias}")

    context.close()
    return data


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    _MATERIA_CACHE.clear()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for clave in PROGRAM_CLAVES:
            try:
                data = scrape_program_json(browser, clave)
            except Exception as e:
                print(f"  [ERROR] No se pudo procesar el programa {clave}: {e}")
                print("  Saltando a la siguiente clave...")
                time.sleep(DELAY_SECONDS)
                continue

            out_path = OUTPUT_DIR / f"{safe_filename(clave)}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"  JSON guardado: {out_path}")

            time.sleep(DELAY_SECONDS)

        browser.close()

    print("\nListo. Revisa la carpeta 'planes_json/'.")


if __name__ == "__main__":
    main()