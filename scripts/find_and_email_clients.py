#!/usr/bin/env python3
"""
Buscador y Contactador de Potenciales Clientes para Griito.
Ejecuta búsquedas por rubro usando Claude API y envía correos de presentación.

Uso:
  export ANTHROPIC_API_KEY="sk-ant-..."
  export SMTP_USERNAME="tu@email.com"
  export SMTP_PASSWORD="tu_password"

  python scripts/find_and_email_clients.py
  python scripts/find_and_email_clients.py --dry-run   # sin enviar correos
  python scripts/find_and_email_clients.py --leads 20   # solo 20 leads
"""

import os
import sys
import json
import sqlite3
import smtplib
import logging
import argparse
import random
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import List, Dict, Optional
import anthropic

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "settings.json"
DB_PATH = BASE_DIR / "data" / "tracking.db"
TEMPLATE_PATH = BASE_DIR / "templates" / "email_template.html"

logger = logging.getLogger("griito_finder")

SECTOR_LABELS = {
    "call_centers": "Centros de Contacto y BPO",
    "seguros": "Compañías de Seguros",
    "bancos_fintech": "Bancos y Fintech",
    "salud_clinicas": "Salud y Clínicas",
    "bienes_raices": "Bienes Raíces",
    "cobranzas": "Cobranzas",
    "telecomunicaciones": "Telecomunicaciones",
    "retail_ecommerce": "Retail y E-commerce",
    "educacion": "Educación y Capacitación",
    "viajes_turismo": "Viajes y Turismo",
    "automotriz": "Automotriz",
    "servicios_publicos": "Servicios Públicos",
    "agencias_marketing": "Agencias de Marketing",
    "logistica_delivery": "Logística y Delivery",
}

CLAUDE_SYSTEM_PROMPT = """Eres un asistente de prospección comercial para Griito, una plataforma de contactabilidad telefónica chilena.

Tu tarea es generar listas de empresas reales que podrían ser clientes potenciales de Griito en Chile.

Para cada empresa, debes proporcionar:
- Nombre de la empresa
- Rubro/sector específico
- Descripción breve de qué hace
- Ciudad
- Correo de contacto (si lo conoces, si no, omítelo)

IMPORTANTE:
- Solo empresas CHILENAS, no incluyas empresas de otros países
- Empresas REALES que existan en el mercado chileno
- Prioriza empresas medianas (50-500 empleados) — EVITA multinacionales y corporaciones gigantes
- Deben claramente necesitar comunicación telefónica con clientes (call centers, notificaciones, recordatorios, cobranzas, agendamiento)
- No repitas empresas de listas anteriores
- Responde SOLO con el JSON, sin texto adicional"""

CLAUDE_USER_PROMPT = """Genera una lista de {count} empresas chilenas reales en el rubro de {sector} que podrían necesitar servicios de contactabilidad telefónica (llamadas automatizadas, notificaciones por voz, recordatorios, campañas de marketing telefónico, etc.).

IMPORTANTE: Solo empresas de CHILE. Empresas medianas (ni microempresas ni multinacionales). Evita filiales de grandes corporaciones globales.

Para cada empresa, proporciona: nombre, rubro específico, descripción breve, ciudad (en Chile), y correo de contacto si lo conoces.

Formato JSON:
```json
[
  {{
    "nombre": "Nombre de la Empresa",
    "rubro": "Rubro específico",
    "descripcion": "Breve descripción de la empresa",
    "pais": "Chile",
    "ciudad": "Santiago",
    "email": "contacto@empresa.cl"
  }}
]
```"""

QC_SYSTEM_PROMPT = """Eres un evaluador de calidad de prospección comercial para Griito, una plataforma de contactabilidad telefónica chilena.

Tu tarea es revisar leads generados automáticamente y puntuar su calidad objetivamente.

Criterios de evaluación:
- real (1-10): ¿Es una empresa REAL y conocida en Chile? 10 = empresa real muy conocida, 1 = inventada
- relevante (1-10): ¿Necesita claramente servicios de contactabilidad telefónica? 10 = necesidad clara y alta, 1 = no necesita
- contacto (1-10): ¿El email parece válido y probablemente llega a quien corresponde? 10 = email directo y válido, 1 = inválido o genérico

Responde SOLO con JSON, sin texto adicional."""

QC_USER_PROMPT = """Evalúa la calidad de estos leads de prospección comercial generados automáticamente para Griito (contactabilidad telefónica).

{leads_json}

Para cada lead, asigna puntaje 1-10 en los criterios: real, relevante, contacto.
Calcula el promedio de los 3.

Responde SOLO con un array JSON:
```json
[
  {{
    "nombre": "Nombre exacto del lead",
    "real": 8,
    "relevante": 7,
    "contacto": 9,
    "promedio": 8.0
  }}
]
```"""


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_DIR / "data" / "client_finder.log", encoding="utf-8"),
        ],
    )


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            rubro TEXT NOT NULL,
            sector TEXT NOT NULL,
            descripcion TEXT,
            pais TEXT DEFAULT 'Chile',
            ciudad TEXT,
            email_contacto TEXT,
            email_enviado TEXT,
            fecha_contacto TIMESTAMP,
            estado TEXT DEFAULT 'pendiente',
            notas TEXT,
            UNIQUE(nombre, sector)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_log (
            sector TEXT PRIMARY KEY,
            ultima_busqueda TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            destinatario TEXT,
            asunto TEXT,
            fecha_envio TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            estado TEXT DEFAULT 'enviado',
            error TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha_revision TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            muestra_total INTEGER,
            puntaje_real REAL,
            puntaje_relevante REAL,
            puntaje_contacto REAL,
            puntaje_promedio REAL,
            historial_promedio REAL,
            alerta_enviada INTEGER DEFAULT 0,
            detalle TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def get_db():
    return sqlite3.connect(str(DB_PATH))


def get_next_sector(config: dict, conn) -> Optional[str]:
    industries = config["company_info"]["industries"]
    min_days = config["script"]["min_days_before_repeat_sector"]
    cutoff = datetime.now() - timedelta(days=min_days)

    cursor = conn.execute(
        "SELECT sector FROM sector_log WHERE ultima_busqueda > ?",
        (cutoff.isoformat(),)
    )
    recent = {row[0] for row in cursor.fetchall()}

    available = [s for s in industries if s not in recent]
    if not available:
        available = industries

    random.shuffle(available)
    return available[0]


def record_sector_search(conn, sector: str):
    conn.execute(
        "INSERT OR REPLACE INTO sector_log (sector, ultima_busqueda) VALUES (?, ?)",
        (sector, datetime.now().isoformat())
    )
    conn.commit()


def fetch_leads_from_claude(client: anthropic.Anthropic, config: dict, sector: str, count: int) -> List[Dict]:
    sector_label = SECTOR_LABELS.get(sector, sector)
    prompt = CLAUDE_USER_PROMPT.format(count=count, sector=sector_label)
    model = config["claude"]["model"]

    logger.info(f"Consultando Claude API ({model}) para sector: {sector_label} ({count} leads)")

    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.content[0].text.strip()

    json_start = content.find("[")
    json_end = content.rfind("]") + 1
    if json_start >= 0 and json_end > json_start:
        content = content[json_start:json_end]

    try:
        leads = json.loads(content)
        logger.info(f"Claude devolvió {len(leads)} leads")
        return leads
    except json.JSONDecodeError as e:
        logger.error(f"Error decodificando JSON de Claude: {e}")
        logger.debug(f"Respuesta raw: {content[:500]}")
        return []


def filter_new_leads(conn, leads: List[Dict], sector: str) -> List[Dict]:
    new_leads = []
    for lead in leads:
        nombre = lead.get("nombre", "").strip()
        if not nombre:
            continue
        existing = conn.execute(
            "SELECT id FROM leads WHERE nombre = ? AND sector = ?",
            (nombre, sector)
        ).fetchone()
        if not existing:
            new_leads.append(lead)
    logger.info(f"Leads nuevos después de filtrar duplicados: {len(new_leads)}/{len(leads)}")
    return new_leads


def save_leads(conn, leads: List[Dict], sector: str):
    for lead in leads:
        conn.execute(
            """INSERT OR IGNORE INTO leads
               (nombre, rubro, sector, descripcion, pais, ciudad, email_contacto, fecha_contacto, estado)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead.get("nombre", "").strip(),
                lead.get("rubro", sector),
                sector,
                lead.get("descripcion", ""),
                lead.get("pais", "Chile"),
                lead.get("ciudad", ""),
                lead.get("email", ""),
                datetime.now().isoformat(),
                "nuevo",
            )
        )
    conn.commit()
    logger.info(f"Guardados {len(leads)} leads en la base de datos")


def load_template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def render_email(template: str, company_name: str, sector_name: str) -> str:
    html = template.replace("{{ company_name }}", company_name)
    html = html.replace("{{ sector_name }}", sector_name)
    return html


def send_email(
    config: dict,
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    dry_run: bool = False,
) -> bool:
    email_cfg = config["email"]
    smtp_user = email_cfg["smtp_username"] or os.environ.get("SMTP_USERNAME", "")
    smtp_pass = email_cfg["smtp_password"] or os.environ.get("SMTP_PASSWORD", "")

    if not smtp_user or not smtp_pass:
        logger.warning("SMTP no configurado. Usando modo dry-run forzado.")
        dry_run = True

    if dry_run:
        logger.info(f"[DRY-RUN] Enviaría correo a: {to_email} ({to_name})")
        logger.info(f"[DRY-RUN] Asunto: {subject}")
        return True

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{email_cfg['from_name']} <{email_cfg['from_email']}>"
    msg["To"] = f"{to_name} <{to_email}>"
    msg["Subject"] = subject

    text_part = MIMEText(
        f"Hola {to_name},\n\nTe escribimos de Griito para presentarte nuestros servicios...",
        "plain", "utf-8"
    )
    html_part = MIMEText(html_body, "html", "utf-8")
    msg.attach(text_part)
    msg.attach(html_part)

    try:
        server = smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"])
        server.ehlo()
        if email_cfg.get("use_tls", True):
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_cfg["from_email"], [to_email], msg.as_string())
        server.quit()
        logger.info(f"Correo enviado a {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error enviando correo a {to_email}: {e}")
        return False


def record_sent_email(conn, lead_id: int, email: str, subject: str, success: bool, error: str = ""):
    estado = "enviado" if success else "error"
    conn.execute(
        "INSERT INTO sent_emails (lead_id, destinatario, asunto, estado, error) VALUES (?, ?, ?, ?, ?)",
        (lead_id, email, subject, estado, error)
    )
    if success:
        conn.execute(
            "UPDATE leads SET estado = 'contactado', email_enviado = ? WHERE id = ?",
            (email, lead_id)
        )
    conn.commit()


def get_lead_email(lead: Dict) -> Optional[str]:
    email = lead.get("email", "").strip()
    if email and "@" in email and "." in email.split("@")[-1]:
        return email
    return None


PIPELINE_DB = "/var/www/pipeline/pipeline.db"


def sync_to_pipeline(lead: Dict, sector_label: str):
    """Sincroniza un lead contactado al pipeline de ventas (SQLite directo)."""
    pipeline_path = os.environ.get("PIPELINE_DB", PIPELINE_DB)
    if not os.path.exists(pipeline_path):
        logger.warning(f"Pipeline DB no encontrada en {pipeline_path}, se omite sincronización")
        return

    try:
        pdb = sqlite3.connect(pipeline_path, timeout=10)
        pdb.execute("PRAGMA journal_mode=WAL")
        pdb.execute("PRAGMA busy_timeout=10000")
        cur = pdb.cursor()
        empresa = lead.get("nombre", "").strip()
        email = lead.get("email", "").strip()
        rubro = lead.get("rubro", sector_label)
        desc = lead.get("descripcion", "").strip()

        if not empresa:
            cur.close()
            pdb.close()
            return

        existing = cur.execute(
            "SELECT id FROM prospects WHERE empresa = ? AND email = ?",
            (empresa, email)
        ).fetchone()
        if existing:
            cur.close()
            pdb.close()
            return

        cur.execute(
            """INSERT INTO prospects (empresa, contacto, email, sector, notas, estado)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (empresa, "", email, rubro, f"Lead generado por Griito Finder. {desc}")
        )
        pid = cur.lastrowid
        cur.execute(
            "INSERT INTO history (prospect_id, estado_nuevo, nota) VALUES (?, 1, ?)",
            (pid, "Contactado automáticamente por Griito Finder")
        )
        pdb.commit()
        cur.close()
        pdb.close()
        logger.info(f"Sincronizado al pipeline: {empresa}")
    except Exception as e:
        logger.error(f"Error sincronizando al pipeline: {e}")


QC_SAMPLE_TEMPLATE = """  - {nombre}: {descripcion} | rubro: {rubro} | ciudad: {ciudad} | email: {email}"""


def get_quality_sample(conn, days_back: int = 7, max_sample: int = 10) -> List[Dict]:
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    rows = conn.execute(
        """SELECT nombre, rubro, descripcion, ciudad, email_contacto
           FROM leads WHERE fecha_contacto > ? AND email_contacto IS NOT NULL AND email_contacto != ''
           ORDER BY fecha_contacto DESC LIMIT ?""",
        (cutoff, max_sample)
    ).fetchall()
    return [{"nombre": r[0], "rubro": r[1], "descripcion": r[2], "ciudad": r[3], "email": r[4]} for r in rows]


def send_alert_email(config: dict, subject: str, body: str):
    email_cfg = config["email"]
    smtp_user = email_cfg["smtp_username"] or os.environ.get("SMTP_USERNAME", "")
    smtp_pass = email_cfg["smtp_password"] or os.environ.get("SMTP_PASSWORD", "")
    alert_to = config.get("quality_control", {}).get("alert_email", "esteban@prioridad.cl")

    if not smtp_user or not smtp_pass:
        logger.warning(f"SMTP no configurado, no se pudo enviar alerta a {alert_to}")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{email_cfg['from_name']} <{email_cfg['from_email']}>"
        msg["To"] = alert_to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        html = f"<html><body style='font-family:Arial,sans-serif;padding:20px'><pre style='white-space:pre-wrap'>{body}</pre></body></html>"
        msg.attach(MIMEText(html, "html", "utf-8"))

        server = smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"])
        server.ehlo()
        if email_cfg.get("use_tls", True):
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_cfg["from_email"], [alert_to], msg.as_string())
        server.quit()
        logger.info(f"Alerta enviada a {alert_to}: {subject}")
    except Exception as e:
        logger.error(f"Error enviando alerta: {e}")


def run_quality_check(client: anthropic.Anthropic, config: dict, conn) -> bool:
    qc = config.get("quality_control", {})
    if not qc.get("enabled", True):
        return False

    qc_model = qc.get("model", "claude-sonnet-4-6")
    sample_size = qc.get("sample_size", 10)
    alert_threshold = qc.get("alert_threshold", 0.2)
    min_days = qc.get("min_days_between_checks", 7)

    last_check = conn.execute(
        "SELECT value FROM quality_config WHERE key = 'last_quality_check'"
    ).fetchone()
    if last_check:
        last_date = datetime.fromisoformat(last_check[0])
        if (datetime.now() - last_date).days < min_days:
            logger.info(f"QC ya ejecutado hace {(datetime.now()-last_date).days} días, próximo en {min_days - (datetime.now()-last_date).days} días")
            return False

    sample = get_quality_sample(conn, days_back=7, max_sample=sample_size)
    if not sample:
        logger.info("QC: sin leads recientes para evaluar, se omite")
        return False

    logger.info(f"QC: evaluando {len(sample)} leads con {qc_model}...")
    leads_json = "\n".join(
        QC_SAMPLE_TEMPLATE.format(**s) for s in sample
    )
    prompt = QC_USER_PROMPT.format(leads_json=leads_json)

    try:
        response = client.messages.create(
            model=qc_model,
            max_tokens=4000,
            system=QC_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text.strip()
        json_start = content.find("[")
        json_end = content.rfind("]") + 1
        if json_start >= 0 and json_end > json_start:
            content = content[json_start:json_end]

        scores = json.loads(content)
        logger.info(f"QC: Claude devolvió {len(scores)} evaluaciones")
    except Exception as e:
        logger.error(f"QC: error evaluando con Claude: {e}")
        return False

    if not scores:
        logger.warning("QC: sin evaluaciones, se omite")
        return False

    total = len(scores)
    avg_real = sum(s.get("real", 0) for s in scores) / total
    avg_relevante = sum(s.get("relevante", 0) for s in scores) / total
    avg_contacto = sum(s.get("contacto", 0) for s in scores) / total
    avg_promedio = sum(s.get("promedio", 0) for s in scores) / total

    historial = conn.execute(
        "SELECT puntaje_promedio FROM quality_log ORDER BY id DESC LIMIT 4"
    ).fetchall()
    historial_promedio = sum(r[0] for r in historial) / len(historial) if historial else avg_promedio

    detalle = json.dumps(scores, ensure_ascii=False, indent=2)

    conn.execute(
        """INSERT INTO quality_log
           (muestra_total, puntaje_real, puntaje_relevante, puntaje_contacto, puntaje_promedio, historial_promedio, detalle)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (total, avg_real, avg_relevante, avg_contacto, avg_promedio, historial_promedio, detalle)
    )
    conn.execute(
        "INSERT OR REPLACE INTO quality_config (key, value) VALUES ('last_quality_check', ?)",
        (datetime.now().isoformat(),)
    )
    conn.commit()

    logger.info(f"QC: promedio={avg_promedio:.1f} | real={avg_real:.1f} | relevante={avg_relevante:.1f} | contacto={avg_contacto:.1f}")

    if historial:
        caida = (historial_promedio - avg_promedio) / historial_promedio if historial_promedio > 0 else 0
        logger.info(f"QC: historial promedio={historial_promedio:.1f}, caída={caida*100:.1f}%")

        if caida > alert_threshold:
            subject = f"⚠️ Alerta de Calidad - Griito Finder (caída de {caida*100:.0f}%)"
            body = f"""ALERTA DE CALIDAD - GENERACIÓN DE LEADS

La calidad de los prospectos ha disminuido significativamente.

Promedio actual:     {avg_promedio:.1f}/10
Historial (4 sem):   {historial_promedio:.1f}/10
Caída:               {caida*100:.1f}%

Desglose:
  - Empresas reales:     {avg_real:.1f}/10
  - Relevancia telefónica: {avg_relevante:.1f}/10
  - Calidad de contacto: {avg_contacto:.1f}/10

Muestra evaluada: {total} leads
Modelo QC: {qc_model}

Acción recomendada: Revisar el prompt de generación o cambiar de modelo.
Próximo paso: Evaluar si haiku sigue siendo adecuado para este sector.

--
Griito Finder - Control de Calidad Semanal"""
            send_alert_email(config, subject, body)
            conn.execute(
                "UPDATE quality_log SET alerta_enviada = 1 WHERE id = (SELECT MAX(id) FROM quality_log)"
            )
            conn.commit()
            logger.info(f"QC: alerta enviada por caída de {caida*100:.0f}%")
            return True

    logger.info("QC: calidad dentro del umbral aceptable")
    return True


def main():
    parser = argparse.ArgumentParser(description="Buscador de clientes potenciales para Griito")
    parser.add_argument("--dry-run", action="store_true", help="No enviar correos realmente")
    parser.add_argument("--leads", type=int, default=None, help="Máximo de leads a contactar")
    parser.add_argument("--sector", type=str, default=None, help="Rubro específico a buscar")
    parser.add_argument("--skip-qc", action="store_true", help="Saltar control de calidad semanal")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config["script"]["log_level"])
    init_db()

    dry_run = args.dry_run or config["script"].get("dry_run", False)
    max_leads = args.leads or config["script"]["leads_per_run"]
    leads_per_sector = config["script"]["leads_per_sector"]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY no configurada. Exporta la variable de entorno.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    conn = get_db()

    if not args.skip_qc:
        try:
            run_quality_check(client, config, conn)
        except Exception as e:
            logger.error(f"Error en control de calidad: {e}")

    total_sent = 0
    processed_sectors = 0
    max_sectors = len(config["company_info"]["industries"])
    sector_override = args.sector

    while total_sent < max_leads and processed_sectors < max_sectors:
        if sector_override:
            sector = sector_override
        else:
            sector = get_next_sector(config, conn)

        if not sector:
            logger.info("No hay más sectores disponibles")
            break

        processed_sectors += 1
        sector_label = SECTOR_LABELS.get(sector, sector)
        logger.info(f"Procesando sector ({processed_sectors}/{max_sectors}): {sector_label}")

        leads = fetch_leads_from_claude(client, config, sector, leads_per_sector)
        if not leads:
            logger.warning(f"No se obtuvieron leads para {sector_label}, pasando al siguiente")
            if sector_override:
                break
            continue

        new_leads = filter_new_leads(conn, leads, sector)
        save_leads(conn, new_leads, sector)
        record_sector_search(conn, sector)

        template = load_template()
        needed = max_leads - total_sent

        for lead in new_leads:
            if total_sent >= max_leads:
                break

            email = get_lead_email(lead)
            company_name = lead.get("nombre", "").strip()

            if not email:
                logger.info(f"Sin email para {company_name}, se omite envío")
                conn.execute(
                    "UPDATE leads SET estado = 'sin_email' WHERE nombre = ? AND sector = ?",
                    (company_name, sector)
                )
                conn.commit()
                continue

            subject = f"{config['company_info']['name']} - Contactabilidad telefónica para {company_name}"
            html_body = render_email(template, company_name, sector_label)

            logger.info(f"Enviando correo a {company_name} <{email}>")
            success = send_email(config, email, company_name, subject, html_body, dry_run)

            lead_row = conn.execute(
                "SELECT id FROM leads WHERE nombre = ? AND sector = ?",
                (company_name, sector)
            ).fetchone()

            if lead_row:
                record_sent_email(conn, lead_row[0], email, subject, success)

            if success:
                total_sent += 1
                sync_to_pipeline(lead, sector_label)

        if sector_override:
            break

    conn.close()
    logger.info(f"Proceso completado. Total de correos {'simulados' if dry_run else 'enviados'}: {total_sent}")

    if not dry_run:
        try:
            _send_daily_summary(config, total_sent, processed_sectors)
        except Exception as e:
            logger.error(f"Error enviando resumen diario: {e}")


def _send_daily_summary(config: dict, total_sent: int, sectors_used: int):
    email_cfg = config["email"]
    smtp_user = email_cfg["smtp_username"] or os.environ.get("SMTP_USERNAME", "")
    smtp_pass = email_cfg["smtp_password"] or os.environ.get("SMTP_PASSWORD", "")
    if not smtp_user or not smtp_pass:
        return

    import sqlite3 as _sql
    conn = _sql.connect(str(DB_PATH))
    total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    total_contactados = conn.execute("SELECT COUNT(*) FROM leads WHERE estado='contactado'").fetchone()[0]
    sin_email = conn.execute("SELECT COUNT(*) FROM leads WHERE estado='sin_email'").fetchone()[0]
    total_enviados = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE estado='enviado'").fetchone()[0]
    errores = conn.execute("SELECT COUNT(*) FROM sent_emails WHERE estado='error'").fetchone()[0]
    ultimo_qc = conn.execute("SELECT puntaje_promedio FROM quality_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    fecha = datetime.now().strftime("%d/%m/%Y")
    qc_line = f"QC promedio: {ultimo_qc[0]:.1f}/10" if ultimo_qc else "QC: sin datos aún"
    body = f"""Resumen Griito Finder - {fecha}

Correos enviados hoy:  {total_sent}
Sectores procesados:  {sectors_used}

Base de datos (histórico):
  Total leads generados:  {total_leads}
  Contactados:            {total_contactados}
  Sin email:              {sin_email}
  Correos enviados:       {total_enviados}
  Errores de envío:       {errores}
  {qc_line}

Pipeline: /var/www/pipeline/
Log:      data/cron.log
Próxima ejecución: {fecha} 09:00
"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{email_cfg['from_name']} <{email_cfg['from_email']}>"
    msg["To"] = config.get("quality_control", {}).get("alert_email", "esteban@prioridad.cl")
    msg["Subject"] = f"📊 Griito Finder - Resumen {fecha} ({total_sent} enviados)"
    html = f"<html><body style='font-family:monospace;padding:20px'><pre style='white-space:pre-wrap'>{body}</pre></body></html>"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        server = smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"])
        server.ehlo()
        if email_cfg.get("use_tls", True):
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_cfg["from_email"], [msg["To"]], msg.as_string())
        server.quit()
        logger.info(f"Resumen diario enviado a {msg['To']}")
    except Exception as e:
        logger.error(f"Error enviando resumen: {e}")


if __name__ == "__main__":
    main()
