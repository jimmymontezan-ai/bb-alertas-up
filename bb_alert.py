import asyncio, os, re, json, pytz, urllib.request, urllib.parse
from datetime import datetime
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
    html_snippet = (await page.content())[:3000]
    print(f"[LOGIN] HTML inicial:\n{html_snippet}")
    username_selector = None
    for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                "input[name='login']", "input[type='text']:visible", "input[autocomplete='username']"]:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                username_selector = sel
                print(f"[LOGIN] Username selector encontrado: {sel}")
                break
        except Exception:
            pass
    if not username_selector:
        direct_login = BB_URL.rstrip("/") + "/webapps/login/"
        print(f"[LOGIN] Navegando a URL directa: {direct_login}")
        try:
            await page.goto(direct_login, wait_until="load", timeout=60000)
        except Exception as e2:
            print(f"[LOGIN] goto direct fallo ({e2})")
            await page.goto(direct_login, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        print(f"[LOGIN] URL directa actual: {page.url}")
        html_snippet2 = (await page.content())[:3000]
        print(f"[LOGIN] HTML directa:\n{html_snippet2}")
        for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                    "input[name='login']", "input[type='text']:visible", "input[autocomplete='username']"]:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    username_selector = sel
                    print(f"[LOGIN] Username selector en directa: {sel}")
                    break
            except Exception:
                pass
    if not username_selector:
        print("[LOGIN] Esperando 15s por renderizado JS...")
        await page.wait_for_timeout(15000)
        html_snippet3 = (await page.content())[:3000]
        print(f"[LOGIN] HTML tras espera:\n{html_snippet3}")
        inputs_info = await page.evaluate(
            "() => Array.from(document.querySelectorAll('input')).map(i => ({id:i.id,name:i.name,type:i.type,placeholder:i.placeholder}))"
        )
        print(f"[LOGIN] Todos los inputs encontrados: {json.dumps(inputs_info)}")
        for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                    "input[name='login']", "input[type='text']", "input[autocomplete='username']"]:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    username_selector = sel
                    print(f"[LOGIN] Username selector (ultimo intento): {sel}")
                    break
            except Exception:
                pass
    if not username_selector:
        raise Exception("No se encontro formulario de login. Ver HTML en logs.")
    password_selector = None
    for sel in ["#pass", "input[name='password']", "input[type='password']"]:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                password_selector = sel
                print(f"[LOGIN] Password selector: {sel}")
                break
        except Exception:
            pass
    if not password_selector:
        password_selector = "input[type='password']"
    try:
        ov = page.locator("div.lb-wrapper[role='dialog']")
        if await ov.count() > 0:
            print("[LOGIN] Cerrando overlay...")
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
                print(f"[LOGIN] Submit via {submit_sel}")
                break
        except Exception:
            pass
    if not submitted:
        print("[LOGIN] Submit via Enter")
        await page.locator(password_selector).press("Enter")
    await page.wait_for_load_state("networkidle", timeout=60000)
    print(f"[LOGIN] Login completado. URL: {page.url}")

# ---------------------------------------------------------------------------
async def get_all_courses(page):
    print("[CURSOS] Buscando cursos...")
    courses = []
    try:
        api_url = BB_URL.rstrip("/") + "/learn/api/public/v1/courses?availability.available=Yes&fields=id,name,courseId&limit=100"
        resp = await page.evaluate(
            "fetch('" + api_url.replace("'", "\\'") + "', {credentials:'include'})"
            ".then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({error:e.toString()}))"
        )
        data = json.loads(resp)
        if "results" in data:
            for c in data["results"]:
                courses.append({"id": c["id"], "name": c.get("name", c.get("courseId", "?"))})
            print(f"[CURSOS] {len(courses)} cursos via API")
            return courses
    except Exception as e:
        print(f"[CURSOS] API error: {e}")
    try:
        await page.goto(BB_URL.rstrip("/") + "/ultra/stream", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        links = await page.evaluate(
            """() => {
                const a = document.querySelectorAll('a[href*="/ultra/courses/"]');
                const seen = new Set(); const res = [];
                a.forEach(el => {
                    const m = el.href.match(/\\/ultra\\/courses\\/([^/]+)/);
                    if (m && !seen.has(m[1])) {
                        seen.add(m[1]);
                        res.push({id:m[1], name:el.textContent.trim()||m[1]});
                    }
                });
                return res;
            }"""
        )
        if links:
            courses = links
            print(f"[CURSOS] {len(courses)} cursos via scraping")
    except Exception as e:
        print(f"[CURSOS] scraping error: {e}")
    return courses

# ---------------------------------------------------------------------------
async def get_gradebook_items(page, course_id, course_name):
    """Obtiene tareas con fecha FUTURA via REST API de Blackboard (sin navegar al curso)."""
    print(f"[GRADEBOOK] {course_name}")
    items = []
    lima_tz = pytz.timezone("America/Lima")
    now = datetime.now(lima_tz)

    try:
        api_url = (BB_URL.rstrip("/") +
                   f"/learn/api/public/v1/courses/{course_id}/gradebook/columns"
                   "?fields=id,name,grading&limit=200")
        resp = await page.evaluate(
            "fetch('" + api_url.replace("'", "\\'") + "', {credentials:'include'})"
            ".then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({error:e.toString()}))"
        )
        data = json.loads(resp)

        if "results" not in data:
            print(f"[GRADEBOOK] Sin resultados: {str(data)[:120]}")
            return items

        future = []
        for col in data["results"]:
            grading = col.get("grading", {})
            due_str = grading.get("due", "")
            if not due_str:
                continue
            try:
                due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                if due_dt.tzinfo is None:
                    due_dt = pytz.utc.localize(due_dt)
                due_lima = due_dt.astimezone(lima_tz)
                if due_lima.date() <= now.date():
                    continue              # solo fechas FUTURAS
                future.append({
                    "name": col.get("name", "?"),
                    "due":  due_lima.strftime("%d/%m/%Y"),
                    "_dt":  due_lima,
                })
            except Exception as e:
                print(f"[GRADEBOOK] date parse error: {e}")

        future.sort(key=lambda x: x["_dt"])
        for it in future:
            del it["_dt"]
        items = future
        print(f"[GRADEBOOK] {len(items)} tareas futuras")

    except Exception as e:
        print(f"[GRADEBOOK] error: {e}")

    return items

# ---------------------------------------------------------------------------
def clean_course_name(name: str) -> str:
    """Extrae el nombre legible del curso."""
    # Formato "Codigo • Nombre del curso"
    if "\u2022" in name or "\u2027" in name or " - " in name[:20]:
        if "•" in name:
            parts = name.split("•", 1)
            candidate = parts[-1].strip()
            if len(candidate) >= 5:
                name = candidate
    # Quitar sufijo tipo "-C-MADM91-EPG2025..."
    m = re.search(r"^(.+?)\s*[-\u2013]\s*[A-Z]{1,5}[-_]", name)
    if m:
        candidate = m.group(1).strip()
        if len(candidate) >= 5:
            return candidate[:70]
    return name[:70] if len(name) > 70 else name

# ---------------------------------------------------------------------------
def format_report(all_items: dict, total_courses: int) -> list:
    """Devuelve lista de mensajes Telegram (<= 4000 chars cada uno)."""
    lima_tz = pytz.timezone("America/Lima")
    now     = datetime.now(lima_tz)
    fecha   = now.strftime("%d/%m/%Y %H:%M")

    header = (
        "\U0001f4da <b>Reporte MBA - UP</b>\n"
        f"<i>{fecha} (Lima)</i>\n"
        f"Cursos revisados: {total_courses}\n"
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
    print(f"[TELEGRAM] Token len={len(TELEGRAM_TOKEN)}, inicio={TELEGRAM_TOKEN[:10]}..., ChatID={TELEGRAM_CHAT_ID}")
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
            print(f"[TELEGRAM] HTTP {e.code} {e.reason}: {body}")
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
            courses = await get_all_courses(page)
            if not courses:
                send_telegram(["\U0001f4da Reporte MBA\n\n\u26a0\ufe0f No se encontraron cursos."])
                return
            all_items = {}
            for c in courses:
                items = await get_gradebook_items(page, c["id"], c["name"])
                if items:
                    all_items[c["name"]] = items
            messages = format_report(all_items, len(courses))
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
