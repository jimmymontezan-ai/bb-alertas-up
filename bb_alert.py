import asyncio
import os
import re
import pytz
from datetime import datetime
from playwright.async_api import async_playwright

BB_URL  = os.environ.get("BB_URL",  "https://aulavirtual.up.edu.pe")
BB_USER = os.environ.get("BB_USER", "")
BB_PASS = os.environ.get("BB_PASS", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LIMA_TZ = pytz.timezone("America/Lima")


async def login(page):
    print("[LOGIN] Abriendo pagina de login...")
    await page.goto(BB_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    # Intentar login con formulario estandar
    try:
        await page.fill('input[name="user_id"], input[id="user_id"], input[type="text"]', BB_USER, timeout=10000)
        await page.fill('input[name="password"], input[id="password"], input[type="password"]', BB_PASS, timeout=10000)
        await page.click('button[type="submit"], input[type="submit"], #entry-login', timeout=10000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        print("[LOGIN] Login completado.")
    except Exception as e:
        print(f"[LOGIN] Error en login estandar: {e}")
        raise


async def get_all_courses(page):
    print("[CURSOS] Buscando cursos activos...")
    courses = []

    try:
        await page.goto(BB_URL + "/ultra/course", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        # Buscar todos los enlaces de cursos en la lista de cursos de Blackboard Ultra
        course_links = await page.query_selector_all('a[href*="/ultra/courses/"][href*="/cl/outline"]')

        if not course_links:
            # Intentar selector alternativo
            course_links = await page.query_selector_all('a[href*="courses/"][href*="outline"]')

        for link in course_links:
            href = await link.get_attribute("href")
            # Extraer course_id del href
            match = re.search(r'/courses/([^/]+)/', href)
            if match:
                course_id = match.group(1)
                # Intentar obtener el nombre del curso
                name_el = await link.query_selector('span, div, h3, h4')
                course_name = await name_el.inner_text() if name_el else course_id
                course_name = course_name.strip().replace("\n", " ")
                courses.append({"id": course_id, "name": course_name, "url": href})
                print(f"  Curso encontrado: {course_name} ({course_id})")

    except Exception as e:
        print(f"[CURSOS] Error obteniendo cursos: {e}")

    if not courses:
        print("[CURSOS] No se encontraron cursos por selector. Intentando via API...")
        try:
            # Blackboard Ultra expone datos via API interna
            api_resp = await page.evaluate("""
                async () => {
                    const r = await fetch('/learn/api/public/v1/courses?availability.available=Yes&fields=id,courseId,name&limit=100', {
                        credentials: 'include'
                    });
                    if (!r.ok) return null;
                    return await r.json();
                }
            """)
            if api_resp and "results" in api_resp:
                for c in api_resp["results"]:
                    courses.append({
                        "id": c.get("id", ""),
                        "name": c.get("name", c.get("courseId", "")),
                        "url": f"/ultra/courses/{c.get('id', '')}/cl/outline"
                    })
                    print(f"  Curso via API: {c.get('name', '')} ({c.get('id', '')})")
        except Exception as e2:
            print(f"[CURSOS] Error via API: {e2}")

    print(f"[CURSOS] Total cursos encontrados: {len(courses)}")
    return courses


async def get_gradebook_items(page, course_id, course_name):
    print(f"[GRADEBOOK] Revisando: {course_name}")
    items = []

    try:
        gradebook_url = f"{BB_URL}/ultra/courses/{course_id}/grade/gradebook"
        await page.goto(gradebook_url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        # Buscar columnas/actividades en el gradebook
        rows = await page.query_selector_all('[data-bbtype="column"], .gradebook-row, [class*="gradable"], [class*="activity"]')

        now_lima = datetime.now(LIMA_TZ)

        for row in rows:
            try:
                # Nombre de la actividad
                name_el = await row.query_selector('[class*="title"], [class*="name"], span, div')
                act_name = (await name_el.inner_text()).strip() if name_el else "Actividad sin nombre"

                # Fecha limite
                due_el = await row.query_selector('[class*="due"], [class*="date"], time')
                due_text = ""
                if due_el:
                    due_text = (await due_el.inner_text()).strip()
                    if not due_text:
                        due_text = await due_el.get_attribute("datetime") or ""

                # Verificar si ya tiene nota (si la celda de nota esta vacia = pendiente)
                score_el = await row.query_selector('[class*="score"], [class*="grade"], [aria-label*="grade"]')
                score_text = (await score_el.inner_text()).strip() if score_el else ""

                is_pending = not score_text or score_text in ["-", "", "–", "—"]

                if is_pending and act_name and len(act_name) > 2:
                    items.append({
                        "curso": course_name,
                        "actividad": act_name,
                        "fecha_limite": due_text or "Sin fecha",
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[GRADEBOOK] Error en {course_name}: {e}")

    print(f"  Pendientes encontrados: {len(items)}")
    return items


def format_report(all_items, total_courses):
    now_lima = datetime.now(LIMA_TZ)
    fecha_str = now_lima.strftime("%d/%m/%Y %H:%M")

    if not all_items:
        msg = (
            f"<b>✅ Reporte Semanal MBA - UP</b>\n"
            f"<i>{fecha_str} (hora Lima)</i>\n\n"
            f"🎉 <b>No hay actividades pendientes</b> en ninguno de los {total_courses} cursos.\n\n"
            f"¡Todo al día! 📚"
        )
        return msg

    # Agrupar por curso
    cursos_dict = {}
    for item in all_items:
        c = item["curso"]
        if c not in cursos_dict:
            cursos_dict[c] = []
        cursos_dict[c].append(item)

    lines = [
        f"<b>📋 Reporte Semanal MBA - UP</b>",
        f"<i>{fecha_str} (hora Lima)</i>",
        f"",
        f"Cursos revisados: <b>{total_courses}</b> | Pendientes: <b>{len(all_items)}</b>",
        f"",
    ]

    for curso, actividades in cursos_dict.items():
        lines.append(f"<b>📖 {curso}</b>")
        for act in actividades:
            fecha = act['fecha_limite']
            lines.append(f"  • {act['actividad']}")
            if fecha and fecha != "Sin fecha":
                lines.append(f"    📅 Vence: {fecha}")
        lines.append("")

    lines.append("—")
    lines.append("Bot Alertas UP MBA 🤖")

    return "\n".join(lines)


async def send_telegram(message):
    import urllib.request, urllib.parse, json
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        if result.get("ok"):
            print("[TELEGRAM] Mensaje enviado correctamente.")
        else:
            print(f"[TELEGRAM] Error: {result}")


async def main():
    print("=" * 60)
    print("Bot Alertas MBA - Universidad del Pacifico")
    print("=" * 60)

    if not BB_USER or not BB_PASS:
        raise ValueError("BB_USER y BB_PASS son obligatorios")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_TOKEN y TELEGRAM_CHAT_ID son obligatorios")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        try:
            await login(page)
            courses = await get_all_courses(page)

            if not courses:
                msg = (
                    "⚠️ <b>Bot Alertas MBA UP</b>\n"
                    "No se encontraron cursos activos.\n"
                    "Verifica que la sesion haya iniciado correctamente."
                )
                await send_telegram(msg)
                return

            all_items = []
            for course in courses:
                items = await get_gradebook_items(page, course["id"], course["name"])
                all_items.extend(items)

            report = format_report(all_items, len(courses))
            print("\n--- REPORTE ---")
            print(report)
            print("---------------\n")

            await send_telegram(report)

        except Exception as e:
            print(f"[ERROR] {e}")
            error_msg = f"❌ <b>Error en Bot Alertas MBA UP</b>\n<code>{str(e)[:200]}</code>"
            try:
                await send_telegram(error_msg)
            except Exception:
                pass
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
