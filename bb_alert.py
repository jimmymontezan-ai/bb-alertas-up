import asyncio, os, re, json, pytz, urllib.request, urllib.parse
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

BB_URL           = os.environ.get("BB_URL", "https://aulavirtual.up.edu.pe")
BB_USER          = os.environ.get("BB_USER", "")
BB_PASS          = os.environ.get("BB_PASS", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}')

# ---------------------------------------------------------------------------
async def login(page):
    print("[LOGIN] Abriendo pagina de login...")
    try:
        await page.goto(BB_URL, wait_until="load", timeout=60000)
    except Exception as e:
        await page.goto(BB_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    print(f"[LOGIN] URL: {page.url}")

    username_selector = None
    for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                "input[name='login']", "input[type='text']:visible"]:
        try:
            if await page.locator(sel).count() > 0:
                username_selector = sel
                break
        except Exception:
            pass

    if not username_selector:
        direct_login = BB_URL.rstrip("/") + "/webapps/login/"
        try:
            await page.goto(direct_login, wait_until="load", timeout=60000)
        except Exception:
            await page.goto(direct_login, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                    "input[name='login']", "input[type='text']:visible"]:
            try:
                if await page.locator(sel).count() > 0:
                    username_selector = sel
                    break
            except Exception:
                pass

    if not username_selector:
        raise Exception("No se encontro formulario de login.")

    password_selector = "input[type='password']"
    for sel in ["#pass", "input[name='password']", "input[type='password']"]:
        try:
            if await page.locator(sel).count() > 0:
                password_selector = sel
                break
        except Exception:
            pass

    try:
        if await page.locator("div.lb-wrapper[role='dialog']").count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    await page.fill(username_selector, BB_USER)
    await page.fill(password_selector, BB_PASS)
    submitted = False
    for submit_sel in ["#entry-login", "input[type='submit']", "button[type='submit']",
                       "button:has-text('Iniciar')", "button:has-text('Login')"]:
        try:
            el = page.locator(submit_sel)
            if await el.count() > 0:
                await el.first.click()
                submitted = True
                break
        except Exception:
            pass
    if not submitted:
        await page.locator(password_selector).press("Enter")
    await page.wait_for_load_state("networkidle", timeout=60000)
    print(f"[LOGIN] Completado. URL: {page.url}")

# ---------------------------------------------------------------------------
def find_due_date_recursive(obj, depth=0):
    """Busca recursivamente el primer campo de fecha de entrega en un objeto."""
    if depth > 5 or not isinstance(obj, dict):
        return None
    DUE_KEYS = ['dueDate', 'due', 'endDate', 'end', 'deadline',
                'submissionDate', 'closeDate', 'dateEnd', 'dueDateMonitor']
    for k in DUE_KEYS:
        v = obj.get(k)
        if v and isinstance(v, str) and ISO_RE.match(v):
            return v
    # Recursion into nested dicts (not lists)
    for k, v in obj.items():
        if isinstance(v, dict):
            result = find_due_date_recursive(v, depth + 1)
            if result:
                return result
    return None

def get_nested(obj, *keys):
    """Devuelve valor anidado siguiendo la ruta de claves."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    return obj

_logged_structures = set()

def _localizable_str(val):
    """Extrae texto de un campo localizable de Blackboard (puede ser str u objeto)."""
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, dict):
        for k in ['rawValue', 'displayValue', 'value', 'en', 'es']:
            v = val.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None

def parse_item_deep(item, now, lima_tz):
    """Parsea un item de Blackboard buscando fecha de entrega futura en cualquier nivel."""
    if not isinstance(item, dict):
        return None

    # Excluir registros de matrícula (no son tareas asignadas)
    if 'role' in item and 'userId' in item:
        return None
    # Excluir eventos de analytics
    if 'se_id' in item or 'providerId' in item:
        return None

    due_str = find_due_date_recursive(item)
    if not due_str:
        return None

    # Log estructura del item (solo las primeras veces para no saturar)
    sig = str(sorted(item.keys()))[:60]
    if sig not in _logged_structures and len(_logged_structures) < 5:
        _logged_structures.add(sig)
        top_keys = list(item.keys())[:12]
        print(f"[STRUCT] top keys: {top_keys}")
        # Log raw values of key fields (regardless of type)
        for fld in ['title', 'calendarName', 'calendarNameLocalizable', 'dynamicCalendarItemProps',
                    'itemSourceType', 'itemSourceId', 'calendarId']:
            val = item.get(fld)
            if val is not None:
                print(f"[STRUCT] {fld}={str(val)[:120]}")
        for sub_k in ['source', 'context', 'event', 'course']:
            sub = item.get(sub_k)
            if isinstance(sub, dict):
                print(f"[STRUCT] .{sub_k} keys={list(sub.keys())[:8]} sample={str(sub)[:100]}")

    try:
        due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        if due_dt.tzinfo is None:
            due_dt = pytz.utc.localize(due_dt)
        due_lima = due_dt.astimezone(lima_tz)
        if due_lima.date() <= now.date():
            return None
    except Exception:
        return None

    # ---- Nombre del curso (buscar en varios lugares) ----
    course_name = (
        _localizable_str(item.get('calendarNameLocalizable')) or
        get_nested(item, 'context', 'courseName') or
        get_nested(item, 'context', 'course', 'name') or
        get_nested(item, 'context', 'course', 'displayName') or
        get_nested(item, 'context', 'courseId') or
        get_nested(item, 'course', 'name') or
        get_nested(item, 'course', 'displayName') or
        item.get('calendarName') or item.get('courseName') or
        item.get('courseId') or item.get('calendarId') or
        "Sin curso"
    )

    # ---- Nombre de la tarea ----
    task_name = (
        _localizable_str(get_nested(item, 'dynamicCalendarItemProps', 'title')) or
        _localizable_str(item.get('title')) or
        get_nested(item, 'source', 'title') or
        get_nested(item, 'source', 'name') or
        get_nested(item, 'source', 'displayName') or
        get_nested(item, 'event', 'title') or
        item.get('name') or item.get('columnName') or item.get('displayTitle') or
        "Tarea sin nombre"
    )

    return {
        "course": str(course_name)[:80],
        "task":   str(task_name)[:120],
        "due":    due_lima.strftime("%d/%m/%Y"),
        "_dt":    due_lima,
    }

def parse_items(items, now, lima_tz):
    """Parsea una lista de items; devuelve dict curso->lista de tareas."""
    all_items = {}
    for item in items:
        parsed = parse_item_deep(item, now, lima_tz)
        if not parsed:
            continue
        cn = parsed["course"]
        if cn not in all_items:
            all_items[cn] = []
        all_items[cn].append({"name": parsed["task"], "due": parsed["due"], "_dt": parsed["_dt"]})

    for cn in all_items:
        all_items[cn].sort(key=lambda x: x["_dt"])
        for it in all_items[cn]:
            del it["_dt"]
    return all_items

# ---------------------------------------------------------------------------
def find_arrays_in_json(data, depth=0, prefix=""):
    """Busca recursivamente todos los arrays no vacíos en un JSON."""
    found = []
    if depth > 4:
        return found
    if isinstance(data, list) and len(data) > 0:
        found.append((prefix or "root", data))
    elif isinstance(data, dict):
        for key, val in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(val, list) and len(val) > 0:
                found.append((path, val))
            elif isinstance(val, dict):
                found.extend(find_arrays_in_json(val, depth+1, path))
    return found

# ---------------------------------------------------------------------------
async def capture_pages_and_parse(page, now, lima_tz, pages_to_visit):
    """Navega una lista de páginas capturando todas las respuestas JSON."""
    captured = []
    course_ids = {}  # courseId -> courseName

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = await response.text()
                if len(body) > 20:
                    captured.append({"url": response.url, "body": body})
            except Exception:
                pass

    page.on("response", on_response)

    for ultra_page in pages_to_visit:
        try:
            before = len(captured)
            await page.goto(BB_URL.rstrip("/") + ultra_page,
                            wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(8000)
            for _ in range(4):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)
            print(f"[NAV] {ultra_page}: {len(captured)-before} resp JSON")
        except Exception as e:
            print(f"[NAV] Error {ultra_page}: {e}")

    page.remove_listener("response", on_response)
    print(f"[CAPTURE] Total: {len(captured)} respuestas JSON")

    all_items = {}
    seen = set()

    for resp in captured:
        try:
            data = json.loads(resp["body"])
        except Exception:
            continue

        arrays = find_arrays_in_json(data)
        for key_path, arr in arrays:
            if not arr:
                continue

            # Intentar extraer courseIds de membership responses
            first = arr[0] if isinstance(arr[0], dict) else {}
            if 'courseId' in first or 'courseId' in str(first.get('course', {})):
                for m in arr:
                    cid = m.get('courseId', get_nested(m, 'course', 'id') or '')
                    cname = (get_nested(m, 'course', 'name') or
                             get_nested(m, 'course', 'displayName') or cid)
                    if cid and cid not in course_ids:
                        course_ids[cid] = str(cname)

            # Parsear como tareas con fecha
            parsed = parse_items(arr, now, lima_tz)
            for course, items in parsed.items():
                if course not in all_items:
                    all_items[course] = []
                for it in items:
                    key = (course, it['name'][:40], it['due'])
                    if key not in seen:
                        seen.add(key)
                        all_items[course].append(it)

    return all_items, course_ids

# ---------------------------------------------------------------------------
async def query_courses(page, course_ids, now, lima_tz):
    """Para cada curso navega grades + outline y captura tareas futuras."""
    all_items = {}
    captured = []

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = await response.text()
                if len(body) > 20:
                    captured.append({"url": response.url, "body": body})
            except Exception:
                pass

    page.on("response", on_response)

    courses_list = list(course_ids.items())[:12]
    print(f"[COURSES] Navegando {len(courses_list)} cursos (grades + outline)...")

    for cid, cname in courses_list:
        # Navegar primero la vista de calificaciones del alumno
        for view in ["grades", "outline"]:
            before = len(captured)
            try:
                await page.goto(
                    BB_URL.rstrip("/") + f"/ultra/courses/{cid}/{view}",
                    wait_until="networkidle", timeout=45000
                )
                await page.wait_for_timeout(4000)
                new = len(captured) - before
                if new > 0:
                    print(f"[COURSE] {cname[:25]} /{view}: {new} resp")
                    break   # si grades trajo datos, no hace falta outline
            except Exception as e:
                print(f"[COURSE] Error {cid}/{view}: {e}")

    page.remove_listener("response", on_response)

    # Log URLs de gradebook/calendar para diagnóstico (solo primeras 8)
    seen_urls = set()
    for r in captured:
        u = r["url"]
        if any(k in u for k in ["gradebook", "calendar", "column", "assignment"]):
            short = u.split("?")[0][-80:]
            if short not in seen_urls:
                seen_urls.add(short)
                print(f"[URL] {short}")

    print(f"[COURSES] Total resp: {len(captured)}")

    # Debug: mostrar sample de URLs con 'column' para verificar el formato
    col_sample = [r['url'].split('?')[0] for r in captured if 'column' in r['url'].lower()]
    for u in col_sample[:5]:
        print(f"[COL-URL] {u[-90:]}")

    # Regex para detectar URL de columna individual: .../gradebook/columns/_NNN_1
    COL_RE = re.compile(r'/gradebook/columns/(_\d+_\d+)/?$')

    # Lookup id→nombre de curso
    id_to_name = {cid: cname for cid, cname in course_ids.items()}

    seen = set()

    def resolve_course(raw_cn):
        """Convierte courseId o nombre sucio al nombre limpio del curso."""
        # Si es un Blackboard ID, buscar nombre en id_to_name
        if re.match(r'^_\d+_\d+$', raw_cn):
            raw_cn = id_to_name.get(raw_cn, raw_cn)
        return clean_course_name(raw_cn)

    def add_parsed(parsed_item):
        if not parsed_item:
            return
        cn = resolve_course(parsed_item["course"])
        if not cn or cn == "Sin curso":
            return
        if cn not in all_items:
            all_items[cn] = []
        k = (cn, parsed_item['task'][:40], parsed_item['due'])
        if k not in seen:
            seen.add(k)
            all_items[cn].append({"name": parsed_item['task'], "due": parsed_item['due']})

    for resp in captured:
        url_path = resp["url"].split("?")[0]
        try:
            data = json.loads(resp["body"])
        except Exception:
            continue

        # ── Solo parsear respuestas de columnas individuales (/gradebook/columns/_ID) ──
        # Estas son los únicos objetos que tienen dueDate + name = nombre real del assignment
        if COL_RE.search(url_path) and isinstance(data, dict):
            add_parsed(parse_item_deep(data, now, lima_tz))

    task_total = sum(len(v) for v in all_items.values())
    print(f"[COURSES] {task_total} tareas en columnas de gradebook")
    return all_items

# ---------------------------------------------------------------------------
async def call_calendar_api_direct(page, now, lima_tz):
    """Llama al API interno de calendario de Blackboard directamente via fetch del browser."""
    since = now.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    until = (now.astimezone(pytz.utc) + timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Intentar múltiples endpoints que Blackboard Ultra puede usar
    endpoints = [
        f"/learn/api/v1/calendar/items?calendarType=course&since={since}&until={until}&limit=500",
        f"/learn/api/v1/calendar/items?since={since}&until={until}&limit=500",
        f"/api/v1/calendar/items?calendarType=course&since={since}&until={until}&limit=500",
    ]

    for ep in endpoints:
        try:
            result = await page.evaluate(f"""
            async () => {{
                try {{
                    const resp = await fetch('{ep}', {{credentials: 'include', headers: {{'Accept': 'application/json'}}}});
                    const body = await resp.text();
                    return {{status: resp.status, body: body}};
                }} catch(e) {{
                    return {{error: e.message}};
                }}
            }}
            """)
            status = result.get('status', 0)
            print(f"[CAL-API] {ep[:60]}... → HTTP {status}")
            if status == 200:
                body = result.get('body', '{}')
                data = json.loads(body)
                arrays = find_arrays_in_json(data)
                all_items = {}
                seen = set()
                for _, arr in arrays:
                    parsed = parse_items(arr, now, lima_tz)
                    for course, items in parsed.items():
                        if course not in all_items:
                            all_items[course] = []
                        for it in items:
                            key = (course, it['name'][:40], it['due'])
                            if key not in seen:
                                seen.add(key)
                                all_items[course].append(it)
                total = sum(len(v) for v in all_items.values())
                print(f"[CAL-API] {total} tareas futuras encontradas")
                if total > 0:
                    return all_items
        except Exception as e:
            print(f"[CAL-API] Error: {e}")
    return {}


async def get_upcoming_assignments(page):
    lima_tz = pytz.timezone("America/Lima")
    now = datetime.now(lima_tz)

    # === Paso 1: Stream → session activa + course IDs ===
    print("[P1] Cargando stream (session + course IDs)...")
    all_items, course_ids = await capture_pages_and_parse(
        page, now, lima_tz, ["/ultra/stream"]
    )
    total = sum(len(v) for v in all_items.values())
    print(f"[P1] {total} tareas en stream, {len(course_ids)} cursos identificados")

    # === Paso 2: API directa de calendario con rango 120 días ===
    print("[P2] Consultando API de calendario (120 dias futuros)...")
    cal_items = await call_calendar_api_direct(page, now, lima_tz)
    if cal_items:
        # Combinar con lo encontrado en stream
        for course, items in cal_items.items():
            if course not in all_items:
                all_items[course] = []
            all_items[course].extend(items)
        total = sum(len(v) for v in all_items.values())
        print(f"[P2] Total combinado: {total} tareas")
        return all_items

    # === Paso 3: Fallback — navegar calendario por fechas futuras ===
    future_pages = ["/ultra/calendar"] + [
        f"/ultra/calendar?date={(now + timedelta(days=d)).strftime('%Y-%m-%d')}"
        for d in [14, 42, 70]
    ]
    print("[P3] Fallback: navegando calendario por fechas futuras...")
    cal_page_items, _ = await capture_pages_and_parse(page, now, lima_tz, future_pages)
    if cal_page_items:
        for course, items in cal_page_items.items():
            if course not in all_items:
                all_items[course] = []
            all_items[course].extend(items)

    total = sum(len(v) for v in all_items.values())
    print(f"[P3] {total} tareas tras navegar calendario")
    if total > 0:
        return all_items

    # === Paso 4: Navegar grades de cada curso (siempre ejecutar para diagnóstico) ===
    if course_ids:
        print("[P4] Navegando grades de cursos individuales...")
        course_items = await query_courses(page, course_ids, now, lima_tz)
        total4 = sum(len(v) for v in course_items.values())
        print(f"[P4] {total4} tareas via grades de cursos")
        for course, items in course_items.items():
            if course not in all_items:
                all_items[course] = []
            all_items[course].extend(items)

    total_final = sum(len(v) for v in all_items.values())
    if total_final == 0:
        print("[WARN] Sin tareas encontradas en ninguna fuente.")
    return all_items

# ---------------------------------------------------------------------------
def clean_course_name(name: str) -> str:
    # Formato Blackboard Ultra: "EPG2025_CODE-C-MADM91: Nombre del Curso-C-MADM91-CODE"
    # Extraer la parte después del primer ": "
    if ': ' in name:
        name = name.split(': ', 1)[1].strip()
    # Eliminar códigos de sección al final: "-C-MADM91-EPG2025_..." etc.
    name = re.sub(r'-[A-Z]-[A-Z0-9]{4,}.*$', '', name).strip()
    # Formato alternativo con "•"
    if "•" in name:
        parts = name.split("•", 1)
        candidate = parts[-1].strip()
        if len(candidate) >= 5:
            name = candidate
    return name[:70] if len(name) > 70 else name

# ---------------------------------------------------------------------------
def format_report(all_items: dict) -> list:
    lima_tz = pytz.timezone("America/Lima")
    now     = datetime.now(lima_tz)
    fecha   = now.strftime("%d/%m/%Y %H:%M")

    total = sum(len(v) for v in all_items.values())
    header = (
        "📚 <b>Reporte MBA - UP</b>\n"
        f"<i>{fecha} (Lima)</i>\n"
        f"Tareas pendientes: {total}\n"
    )
    footer = "\n🤖 <i>Bot Alertas MBA - UP</i>"
    MAX = 4000

    courses_with_items = {k: v for k, v in all_items.items() if v}
    if not courses_with_items:
        return [header + "\n✅ <b>No se encontraron tareas con fecha futura.</b>" + footer]

    messages = []
    current  = header
    for raw_name, items in courses_with_items.items():
        cname = clean_course_name(raw_name)
        block = f"\n📖 <b>{cname}</b>\n"
        for it in items:
            block += f"  ⏰ {it['name']} → <b>{it['due']}</b>\n"
        if len(current) + len(block) + len(footer) > MAX:
            messages.append(current + footer)
            current = block
        else:
            current += block
    messages.append(current + footer)
    return messages

# ---------------------------------------------------------------------------
def send_telegram(messages: list):
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN no configurado")
    if not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID no configurado")
    print(f"[TELEGRAM] Enviando {len(messages)} mensaje(s)...")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for msg in messages:
        data = urllib.parse.urlencode({
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read())
                print("[TELEGRAM] OK" if res.get("ok") else f"[TELEGRAM] Error: {res}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[TELEGRAM] HTTP {e.code}: {body}")
            raise

# ---------------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("Bot Alertas MBA - Universidad del Pacifico")
    print("=" * 60)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(60000)
        try:
            await login(page)
            all_items = await get_upcoming_assignments(page)
            messages = format_report(all_items)
            print("\n---\n".join(messages))
            send_telegram(messages)
        except Exception as e:
            import traceback; traceback.print_exc()
            try:
                send_telegram([f"<b>❌ Error Bot MBA</b>\n<code>{str(e)[:200]}</code>"])
            except Exception:
                pass
            raise
        finally:
            await browser.close()

asyncio.run(main())
