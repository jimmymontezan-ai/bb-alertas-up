import asyncio, os, re, json, pytz, urllib.request, urllib.parse
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

BB_URL           = os.environ.get("BB_URL", "https://aulavirtual.up.edu.pe")
BB_USER          = os.environ.get("BB_USER", "")
BB_PASS          = os.environ.get("BB_PASS", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
async def login(page):
    print("[LOGIN] Abriendo pagina de login...")
    try:
        await page.goto(BB_URL, wait_until="load", timeout=60000)
    except Exception as e:
        print(f"[LOGIN] goto 'load' fallo ({e}), reintentando...")
        await page.goto(BB_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    print(f"[LOGIN] URL: {page.url}")
    print(f"[LOGIN] Titulo: {await page.title()}")

    username_selector = None
    for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                "input[name='login']", "input[type='text']:visible", "input[autocomplete='username']"]:
        try:
            if await page.locator(sel).count() > 0:
                username_selector = sel
                print(f"[LOGIN] Username selector: {sel}")
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
        await page.wait_for_timeout(15000)
        for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                    "input[name='login']", "input[type='text']"]:
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
async def get_user_id(page):
    try:
        resp = await page.evaluate(
            "fetch('/learn/api/public/v1/users/me', {credentials:'include'})"
            ".then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({error:e.toString()}))"
        )
        data = json.loads(resp)
        uid = data.get("id", data.get("userId", ""))
        print(f"[USER] id={uid}  data={str(data)[:200]}")
        return uid
    except Exception as e:
        print(f"[USER] error: {e}")
        return ""

# ---------------------------------------------------------------------------
async def try_various_apis(page, user_id, now, lima_tz):
    """Intenta varios endpoints REST y devuelve items si encuentra algo."""
    since = now.strftime("%Y-%m-%dT00:00:00.000Z")
    until = (now + timedelta(days=120)).strftime("%Y-%m-%dT23:59:59.000Z")

    apis = [
        # Calendario (v1, v2)
        f"/learn/api/public/v1/calendar/items?since={since}&until={until}&limit=200",
        f"/learn/api/public/v2/calendar/items?since={since}&until={until}&limit=200",
        # Stream activities
        "/learn/api/public/v1/streams/activities?limit=100",
        "/learn/api/public/v2/streams/activities?limit=100",
        # User memberships
        "/learn/api/public/v1/users/me/memberships?limit=100&fields=courseId,courseRoleId,lastAccessDate",
        f"/learn/api/public/v1/users/{user_id}/memberships?limit=100" if user_id else None,
        # Upcoming / todo
        "/learn/api/public/v1/users/me/upcoming?limit=100",
        "/learn/api/public/v2/users/me/upcoming?limit=100",
        "/learn/api/public/v1/users/me/todo?limit=100",
    ]

    for api in apis:
        if not api:
            continue
        url = BB_URL.rstrip("/") + api
        try:
            resp = await page.evaluate(
                f"fetch('{url}', {{credentials:'include'}})"
                ".then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({error:e.toString()}))"
            )
            data = json.loads(resp)
            status = data.get("status", "")
            count = len(data.get("results", data.get("items", [])))
            print(f"[API] {api[:70]}: status={status} count={count} raw={str(data)[:150]}")

            if count > 0:
                items = data.get("results", data.get("items", []))
                all_items = parse_items(items, now, lima_tz)
                if all_items:
                    return all_items
        except Exception as e:
            print(f"[API] {api[:70]}: error={e}")

    return {}

# ---------------------------------------------------------------------------
def parse_items(items, now, lima_tz):
    """Parsea una lista de items y extrae assignments con fecha futura."""
    all_items = {}
    for item in items:
        # Buscar campo de fecha en varias posibles claves
        due_str = (item.get("end") or item.get("start") or item.get("dueDate") or
                   item.get("due") or item.get("endDate") or "")
        if not due_str:
            continue
        try:
            due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
            if due_dt.tzinfo is None:
                due_dt = pytz.utc.localize(due_dt)
            due_lima = due_dt.astimezone(lima_tz)
            if due_lima.date() <= now.date():
                continue
            course_name = (item.get("calendarName") or item.get("courseName") or
                           item.get("courseId") or item.get("calendarId") or "Sin curso")
            task_name = (item.get("title") or item.get("name") or
                         item.get("columnName") or "?")
            if course_name not in all_items:
                all_items[course_name] = []
            all_items[course_name].append({
                "name": task_name,
                "due":  due_lima.strftime("%d/%m/%Y"),
                "_dt":  due_lima,
            })
        except Exception as e:
            print(f"[PARSE] date error: {e}")

    for cn in all_items:
        all_items[cn].sort(key=lambda x: x["_dt"])
        for it in all_items[cn]:
            del it["_dt"]
    return all_items

# ---------------------------------------------------------------------------
async def intercept_stream_page(page, now, lima_tz):
    """Navega al stream de Ultra e intercepta todas las respuestas JSON de la API."""
    captured = []

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct and any(k in response.url for k in ["/api/", "learn/api", "/ultra/"]):
            try:
                body = await response.text()
                captured.append({"url": response.url, "body": body})
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(BB_URL.rstrip("/") + "/ultra/stream",
                        wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(5000)
        # Scroll para cargar mas contenido
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[STREAM] navigation error: {e}")
    finally:
        page.remove_listener("response", on_response)

    print(f"[INTERCEPT] {len(captured)} respuestas JSON capturadas")

    # Analizar respuestas capturadas
    all_items = {}
    for resp in captured:
        try:
            data = json.loads(resp["body"])
            results = data.get("results", data.get("items", []))
            if not results:
                continue
            print(f"[INTERCEPT] {resp['url'][-80:]}: {len(results)} items sample={str(results[0])[:150]}")
            parsed = parse_items(results, now, lima_tz)
            for course, items in parsed.items():
                if course not in all_items:
                    all_items[course] = []
                all_items[course].extend(items)
        except Exception:
            pass

    if all_items:
        return all_items

    # Si no se obtuvo nada, intentar scraping del DOM
    print("[DOM] Intentando scraping del DOM del stream...")
    return await scrape_stream_dom(page, now, lima_tz)

# ---------------------------------------------------------------------------
async def scrape_stream_dom(page, now, lima_tz):
    """Extrae assignments del DOM renderizado del activity stream."""
    try:
        dom_text = await page.evaluate("""
            () => {
                // Buscar elementos con fechas futuras
                const items = [];

                // Patrones de selectores comunes en Blackboard Ultra
                const containers = document.querySelectorAll(
                    '[class*="activity"], [class*="stream-item"], [class*="card"], ' +
                    '[class*="upcoming"], [class*="grade-row"], article, ' +
                    '[data-is-active], bb-base-stream-entry'
                );

                containers.forEach(el => {
                    const text = el.textContent.trim();
                    // Buscar texto con patrones de fecha
                    if (text.match(/\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{2,4}/) ||
                        text.match(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d/) ||
                        text.match(/Due/i) || text.match(/Entrega/i) || text.match(/Vence/i)) {
                        items.push(text.slice(0, 300));
                    }
                });

                // Tambien buscar elementos time con datetime
                document.querySelectorAll('time[datetime]').forEach(el => {
                    const parent = el.closest('[class]');
                    items.push((parent || el).textContent.trim().slice(0, 300));
                });

                return [...new Set(items)].slice(0, 100);
            }
        """)
        print(f"[DOM] {len(dom_text)} elementos encontrados")
        for i, item in enumerate(dom_text[:20]):
            print(f"[DOM] [{i}] {item[:150]}")
    except Exception as e:
        print(f"[DOM] error: {e}")

    # Si llegamos aqui, no encontramos datos estructurados
    # Devolver vacio - el reporte dira "no hay tareas"
    return {}

# ---------------------------------------------------------------------------
async def get_upcoming_assignments(page):
    """Orquesta todas las estrategias para obtener assignments pendientes."""
    lima_tz = pytz.timezone("America/Lima")
    now     = datetime.now(lima_tz)

    # Paso 1: obtener user ID
    user_id = await get_user_id(page)

    # Paso 2: probar APIs REST directas
    all_items = await try_various_apis(page, user_id, now, lima_tz)
    if all_items:
        print(f"[RESULT] {sum(len(v) for v in all_items.values())} tareas via API directa")
        return all_items

    # Paso 3: interceptar stream + DOM
    all_items = await intercept_stream_page(page, now, lima_tz)
    print(f"[RESULT] {sum(len(v) for v in all_items.values())} tareas via stream/DOM")
    return all_items

# ---------------------------------------------------------------------------
def clean_course_name(name: str) -> str:
    if "\u2022" in name or " - " in name[:20]:
        if "•" in name:
            parts = name.split("•", 1)
            candidate = parts[-1].strip()
            if len(candidate) >= 5:
                name = candidate
    m = re.search(r"^(.+?)\s*[-\u2013]\s*[A-Z]{1,5}[-_]", name)
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
        "\U0001f4da <b>Reporte MBA - UP</b>\n"
        f"<i>{fecha} (Lima)</i>\n"
        f"Tareas encontradas: {total}\n"
    )
    footer = "\n\U0001f916 <i>Bot Alertas MBA - UP</i>"
    MAX    = 4000

    courses_with_items = {k: v for k, v in all_items.items() if v}
    if not courses_with_items:
        return [header + "\n\u2705 <b>No hay tareas con fecha futura.</b>" + footer]

    messages = []
    current  = header
    for raw_name, items in courses_with_items.items():
        cname = clean_course_name(raw_name)
        block = f"\n\U0001f4d6 <b>{cname}</b>\n"
        for it in items:
            block += f"  \u23f0 {it['name']} \u2192 <b>{it['due']}</b>\n"
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
    print(f"[TELEGRAM] Token len={len(TELEGRAM_TOKEN)}, ChatID={TELEGRAM_CHAT_ID}")
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
                send_telegram([f"<b>\u274c Error Bot MBA</b>\n<code>{str(e)[:200]}</code>"])
            except Exception:
                pass
            raise
        finally:
            await browser.close()

asyncio.run(main())
