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

def parse_item_deep(item, now, lima_tz):
    """Parsea un item de Blackboard buscando fecha de entrega futura en cualquier nivel."""
    if not isinstance(item, dict):
        return None

    due_str = find_due_date_recursive(item)
    if not due_str:
        return None

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
        get_nested(item, 'source', 'title') or
        get_nested(item, 'source', 'name') or
        get_nested(item, 'source', 'displayName') or
        get_nested(item, 'event', 'title') or
        item.get('title') or item.get('name') or
        item.get('columnName') or item.get('displayTitle') or
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
    """Para cada curso, navega a su página Ultra y captura datos de tareas."""
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

    courses_list = list(course_ids.items())[:15]  # Max 15 cursos
    print(f"[COURSES] Navegando {len(courses_list)} cursos...")

    for cid, cname in courses_list:
        before = len(captured)
        try:
            await page.goto(
                BB_URL.rstrip("/") + f"/ultra/courses/{cid}/outline",
                wait_until="networkidle", timeout=45000
            )
            await page.wait_for_timeout(5000)
            new = len(captured) - before
            print(f"[COURSE] {cname[:30]}: {new} resp")
        except Exception as e:
            print(f"[COURSE] Error {cid}: {e}")

    page.remove_listener("response", on_response)
    print(f"[COURSES] Total resp: {len(captured)}")

    seen = set()
    for resp in captured:
        try:
            data = json.loads(resp["body"])
        except Exception:
            continue
        arrays = find_arrays_in_json(data)
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

    return all_items

# ---------------------------------------------------------------------------
async def get_upcoming_assignments(page):
    lima_tz = pytz.timezone("America/Lima")
    now = datetime.now(lima_tz)

    # === Paso 1: Stream + Calendar → busca tareas y extrae course IDs ===
    print("[P1] Capturando stream y calendar...")
    all_items, course_ids = await capture_pages_and_parse(
        page, now, lima_tz,
        ["/ultra/stream", "/ultra/calendar"]
    )
    total = sum(len(v) for v in all_items.values())
    print(f"[P1] {total} tareas, {len(course_ids)} cursos identificados")

    if total > 0:
        return all_items

    # === Paso 2: Navegar por cada curso si no encontramos tareas ===
    if course_ids:
        print("[P2] Navegando cursos individualmente...")
        course_items = await query_courses(page, course_ids, now, lima_tz)
        total2 = sum(len(v) for v in course_items.values())
        print(f"[P2] {total2} tareas via cursos individuales")
        if total2 > 0:
            return course_items

    # === Paso 3: Log diagnóstico de lo que hay en la página ===
    print("[P3] Sin tareas encontradas. Logging items de red para debug...")
    return {}

# ---------------------------------------------------------------------------
def clean_course_name(name: str) -> str:
    if "•" in name:
        parts = name.split("•", 1)
        candidate = parts[-1].strip()
        if len(candidate) >= 5:
            name = candidate
    m = re.search(r"^(.+?)\s*[-–]\s*[A-Z]{1,5}[-_]", name)
    if m:
        candidate = m.group(1).strip()
        if len(candidate) >= 5:
            return candidate[:70]
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
