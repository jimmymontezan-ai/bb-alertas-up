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
def find_arrays_in_json(data, depth=0, prefix=""):
    """Busca recursivamente todos los arrays con items en un objeto JSON."""
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
def parse_items(items, now, lima_tz):
    """Parsea una lista de items buscando assignments con fecha futura."""
    all_items = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        # Buscar fecha en muchas posibles claves
        due_str = ""
        for key in ["end", "start", "dueDate", "due", "endDate", "dateEnd",
                    "deadline", "submissionDate", "closeDate", "date",
                    "dateAvailable", "datePublished"]:
            val = item.get(key, "")
            if val and isinstance(val, str) and len(val) >= 8:
                due_str = val
                break

        if not due_str:
            continue
        try:
            due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
            if due_dt.tzinfo is None:
                due_dt = pytz.utc.localize(due_dt)
            due_lima = due_dt.astimezone(lima_tz)
            if due_lima.date() <= now.date():
                continue
            # Buscar nombre del curso
            course_name = ""
            for key in ["calendarName", "courseName", "courseId", "calendarId",
                        "course", "courseTitle", "contextLabel", "context"]:
                val = item.get(key, "")
                if val and isinstance(val, str) and len(val) >= 3:
                    course_name = val
                    break
            if not course_name:
                course_name = "Sin curso"

            # Buscar nombre de la tarea
            task_name = ""
            for key in ["title", "name", "columnName", "description",
                        "subject", "displayTitle", "label"]:
                val = item.get(key, "")
                if val and isinstance(val, str) and len(val) >= 2:
                    task_name = val
                    break
            if not task_name:
                task_name = "Tarea sin nombre"

            if course_name not in all_items:
                all_items[course_name] = []
            all_items[course_name].append({
                "name": task_name,
                "due":  due_lima.strftime("%d/%m/%Y"),
                "_dt":  due_lima,
            })
        except Exception as e:
            pass

    for cn in all_items:
        all_items[cn].sort(key=lambda x: x["_dt"])
        for it in all_items[cn]:
            del it["_dt"]
    return all_items

# ---------------------------------------------------------------------------
async def get_memberships(page):
    """Obtiene los cursos del estudiante via API de memberships."""
    try:
        resp = await page.evaluate("""
            fetch('/learn/api/public/v1/users/me/memberships?limit=200&expand=course', {credentials:'include'})
            .then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({error:e.toString()}))
        """)
        data = json.loads(resp)
        courses = data.get("results", [])
        print(f"[MEMBER] {len(courses)} cursos encontrados, keys={list(data.keys())[:10]}")
        # Extraer course IDs y nombres
        course_info = []
        for c in courses:
            cid = c.get("courseId", c.get("id", ""))
            # Try to get course name from expanded course data
            course_obj = c.get("course", {})
            cname = course_obj.get("name", course_obj.get("displayName", c.get("courseName", cid)))
            if cid:
                course_info.append({"id": cid, "name": cname})
        return course_info
    except Exception as e:
        print(f"[MEMBER] error: {e}")
        return []

# ---------------------------------------------------------------------------
async def get_course_assignments(page, course_id, course_name, now, lima_tz):
    """Obtiene assignments de un curso via su contenido o gradebook."""
    all_items = {}
    since = now.strftime("%Y-%m-%dT00:00:00.000Z")
    until = (now + timedelta(days=120)).strftime("%Y-%m-%dT23:59:59.000Z")

    # Intentar gradebook grades (estudiante puede ver sus propias notas)
    try:
        resp = await page.evaluate(f"""
            fetch('/learn/api/public/v2/courses/{course_id}/gradebook/columns?limit=100', {{credentials:'include'}})
            .then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({{error:e.toString()}}))
        """)
        data = json.loads(resp)
        status = data.get("status", 200)
        if status not in [401, 403, 404]:
            cols = data.get("results", [])
            print(f"[COURSE] {course_id[:20]} gradebook v2: {len(cols)} cols")
            for col in cols:
                due_str = col.get("due", col.get("dueDate", ""))
                if not due_str:
                    continue
                try:
                    due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                    if due_dt.tzinfo is None:
                        due_dt = pytz.utc.localize(due_dt)
                    due_lima = due_dt.astimezone(lima_tz)
                    if due_lima.date() > now.date():
                        if course_name not in all_items:
                            all_items[course_name] = []
                        all_items[course_name].append({
                            "name": col.get("name", col.get("displayName", "Tarea")),
                            "due": due_lima.strftime("%d/%m/%Y"),
                            "_dt": due_lima,
                        })
                except Exception:
                    pass
    except Exception as e:
        pass

    return all_items

# ---------------------------------------------------------------------------
async def intercept_all_pages(page, now, lima_tz):
    """Navega por varias páginas capturando TODAS las respuestas JSON."""
    captured = []

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = await response.text()
                if len(body) > 10:
                    captured.append({"url": response.url, "body": body})
            except Exception:
                pass

    page.on("response", on_response)

    pages_to_try = [
        "/ultra/stream",
        "/ultra/calendar",
    ]

    for ultra_page in pages_to_try:
        try:
            cap_before = len(captured)
            await page.goto(BB_URL.rstrip("/") + ultra_page,
                            wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(8000)
            # Scroll para cargar mas contenido
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            new_captured = len(captured) - cap_before
            print(f"[INTERCEPT] Pagina {ultra_page}: {new_captured} respuestas JSON nuevas")
        except Exception as e:
            print(f"[INTERCEPT] Error en {ultra_page}: {e}")

    page.remove_listener("response", on_response)
    print(f"[INTERCEPT] Total: {len(captured)} respuestas JSON capturadas")

    # Analizar TODAS las respuestas buscando arrays con datos de tareas
    all_items = {}
    seen_url_patterns = set()

    for resp in captured:
        try:
            data = json.loads(resp["body"])
        except Exception:
            continue

        # Buscar TODOS los arrays en la respuesta
        arrays_found = find_arrays_in_json(data)
        for key_path, arr in arrays_found:
            if len(arr) == 0:
                continue
            # Intentar parsear como items de calendario/tarea
            parsed = parse_items(arr, now, lima_tz)
            if parsed:
                url_short = resp["url"][-60:] if len(resp["url"]) > 60 else resp["url"]
                print(f"[INTERCEPT] TAREAS ENCONTRADAS! key='{key_path}' url=...{url_short}")
                for course, items in parsed.items():
                    if course not in all_items:
                        all_items[course] = []
                    all_items[course].extend(items)
            else:
                # Log los primeros arrays con items (para debug)
                url_pattern = resp["url"].split("?")[0][-50:]
                if url_pattern not in seen_url_patterns and len(arr) > 0:
                    sample = arr[0] if isinstance(arr[0], dict) else {}
                    keys = list(sample.keys())[:8] if sample else []
                    print(f"[SCAN] key='{key_path}' n={len(arr)} campos={keys}")
                    seen_url_patterns.add(url_pattern)

    return all_items

# ---------------------------------------------------------------------------
async def extract_shadow_dom_text(page):
    """Extrae todo el texto visible incluyendo Shadow DOM de forma recursiva."""
    try:
        text_data = await page.evaluate("""
            () => {
                const results = [];

                function collectFromRoot(root, depth) {
                    if (depth > 20) return;
                    // Collect text nodes
                    const walker = document.createTreeWalker(
                        root, NodeFilter.SHOW_TEXT, null, false
                    );
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.nodeValue ? node.nodeValue.trim() : '';
                        if (t.length > 1) results.push(t);
                    }
                    // Recurse into shadow roots
                    const allEls = root.querySelectorAll('*');
                    allEls.forEach(el => {
                        if (el.shadowRoot) {
                            collectFromRoot(el.shadowRoot, depth + 1);
                        }
                    });
                }

                collectFromRoot(document, 0);
                return results;
            }
        """)
        print(f"[SHADOW] {len(text_data)} fragmentos de texto extraidos")
        return text_data
    except Exception as e:
        print(f"[SHADOW] error: {e}")
        return []

# ---------------------------------------------------------------------------
def parse_text_for_assignments(text_fragments, now, lima_tz):
    """Parsea una lista de fragmentos de texto buscando fechas y nombres de tareas."""
    # Patrones de fecha comunes
    date_patterns = [
        # dd/mm/yyyy or dd-mm-yyyy
        (r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', 'dmy'),
        # Month dd, yyyy (English)
        (r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})', 'mdy_en'),
        # dd de Month de yyyy (Spanish)
        (r'(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+de\s+(\d{4})', 'dmy_es'),
        # yyyy-mm-dd
        (r'(\d{4})-(\d{2})-(\d{2})', 'ymd'),
    ]

    months_es = {
        'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,
        'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12
    }
    months_en = {
        'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
        'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12
    }

    def try_parse_date(text):
        for pattern, fmt in date_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                try:
                    if fmt == 'dmy':
                        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    elif fmt == 'mdy_en':
                        mo = months_en.get(m.group(1).lower()[:3], 0)
                        d, y = int(m.group(2)), int(m.group(3))
                    elif fmt == 'dmy_es':
                        d = int(m.group(1))
                        mo = months_es.get(m.group(2).lower(), 0)
                        y = int(m.group(3))
                    elif fmt == 'ymd':
                        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    else:
                        continue
                    if mo < 1 or mo > 12 or d < 1 or d > 31:
                        continue
                    dt = datetime(y, mo, d, tzinfo=lima_tz)
                    return dt
                except Exception:
                    continue
        return None

    # Buscar pares texto-con-fecha
    all_items = {}
    # Juntamos fragmentos consecutivos para contexto
    for i, frag in enumerate(text_fragments):
        dt = try_parse_date(frag)
        if not dt:
            continue
        if dt.date() <= now.date():
            continue
        # Buscar nombre de tarea en fragmentos cercanos
        context = text_fragments[max(0, i-5):i+3]
        task_name = ""
        course_name = "Sin curso"
        for ctx in context:
            ctx = ctx.strip()
            # Si el fragmento tiene palabras reales y no es solo fecha
            if (len(ctx) > 5 and not re.match(r'^[\d:/\s]+$', ctx)
                    and 'Due' not in ctx and 'Entrega' not in ctx
                    and 'Vence' not in ctx and len(ctx) < 200):
                if not task_name:
                    task_name = ctx[:80]
                elif not course_name or course_name == "Sin curso":
                    course_name = ctx[:70]

        if task_name and len(task_name) > 3:
            if course_name not in all_items:
                all_items[course_name] = []
            all_items[course_name].append({
                "name": task_name,
                "due": dt.strftime("%d/%m/%Y"),
                "_dt": dt,
            })

    for cn in all_items:
        all_items[cn].sort(key=lambda x: x["_dt"])
        for it in all_items[cn]:
            del it["_dt"]

    # Deduplicar
    for cn in all_items:
        seen = set()
        unique = []
        for it in all_items[cn]:
            key = (it["name"][:40], it["due"])
            if key not in seen:
                seen.add(key)
                unique.append(it)
        all_items[cn] = unique

    return all_items

# ---------------------------------------------------------------------------
async def get_upcoming_assignments(page):
    """Orquesta todas las estrategias para obtener assignments pendientes."""
    lima_tz = pytz.timezone("America/Lima")
    now = datetime.now(lima_tz)

    # === ESTRATEGIA 1: Memberships + gradebook por curso ===
    courses = await get_memberships(page)
    print(f"[STRAT1] {len(courses)} cursos matriculados")

    all_items = {}
    if courses:
        for course in courses[:30]:  # Max 30 cursos para no tardar demasiado
            items = await get_course_assignments(page, course["id"], course["name"], now, lima_tz)
            for cn, tasks in items.items():
                if cn not in all_items:
                    all_items[cn] = []
                all_items[cn].extend(tasks)

    total_s1 = sum(len(v) for v in all_items.values())
    print(f"[STRAT1] {total_s1} tareas encontradas via gradebook")

    if total_s1 > 0:
        return all_items

    # === ESTRATEGIA 2: Intercepción de red en páginas Ultra ===
    print("[STRAT2] Interceptando respuestas de red...")
    all_items = await intercept_all_pages(page, now, lima_tz)
    total_s2 = sum(len(v) for v in all_items.values())
    print(f"[STRAT2] {total_s2} tareas encontradas via intercepcion")

    if total_s2 > 0:
        return all_items

    # === ESTRATEGIA 3: Shadow DOM text extraction ===
    print("[STRAT3] Extrayendo texto via Shadow DOM...")
    # Navegar al stream (ya deberia estar ahi desde estrategia 2)
    try:
        if "/ultra/" not in page.url:
            await page.goto(BB_URL.rstrip("/") + "/ultra/stream",
                           wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(8000)
    except Exception as e:
        print(f"[STRAT3] error navegacion: {e}")

    text_fragments = await extract_shadow_dom_text(page)
    all_items = parse_text_for_assignments(text_fragments, now, lima_tz)
    total_s3 = sum(len(v) for v in all_items.values())
    print(f"[STRAT3] {total_s3} tareas encontradas via shadow DOM text")

    # Log fragmentos con fechas para debug
    if total_s3 == 0:
        date_frags = [f for f in text_fragments if re.search(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{4}-\d{2}-\d{2}', f)]
        print(f"[DEBUG] {len(date_frags)} fragmentos con fechas:")
        for f in date_frags[:20]:
            print(f"  DATE: {f[:100]}")

    return all_items

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
        f"Tareas encontradas: {total}\n"
    )
    footer = "\n🤖 <i>Bot Alertas MBA - UP</i>"
    MAX    = 4000

    courses_with_items = {k: v for k, v in all_items.items() if v}
    if not courses_with_items:
        return [header + "\n✅ <b>No hay tareas con fecha futura.</b>" + footer]

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
