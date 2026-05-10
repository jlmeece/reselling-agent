"""
Tool: alert_sender
Sends HTML email alerts and emoji-coded SMS via carrier email-to-text gateway.
Secrets loaded from .env.

Two visual modes:
  urgent=True  → Red "Action Required" card template + SMS
  urgent=False → Green/neutral summary template, email only
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from loguru import logger


# ── HTML Templates ────────────────────────────────────────────────────────────

def _html_urgent(subject, items, sheet_url, run_time):
    """
    Red card template. items = list of dicts with keys:
      title, row, category, reason, reprice_note (optional)
    """
    cards = ""
    for item in items:
        reprice = f'<br><span style="font-size:13px;color:#1d1d1f">💰 {item.get("reprice_note","")}</span>' if item.get("reprice_note") else ""
        cards += f"""
        <div style="background:#fff3f3;border-left:4px solid #ff3b30;padding:12px 16px;margin:8px 0;border-radius:0 6px 6px 0">
          <b style="font-size:15px;color:#1d1d1f">{item['title']}</b><br>
          <span style="color:#888;font-size:12px">Row {item['row']} · {item.get('category','')}</span><br>
          <span style="color:#ff3b30;font-size:13px;font-weight:600">{item['reason']}</span>
          {reprice}
        </div>"""

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:16px;background:#f2f2f7;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif">
<div style="max-width:600px;margin:0 auto">

  <div style="background:#ff3b30;padding:16px 24px;border-radius:10px 10px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">⚠ Action Required</span>
    <span style="color:rgba(255,255,255,0.75);font-size:12px;float:right;line-height:26px">{run_time}</span>
  </div>

  <div style="background:#fff;border:1px solid #e5e5ea;border-top:none;padding:16px 24px;border-radius:0 0 10px 10px">
    <p style="margin:0 0 12px;font-size:14px;color:#3a3a3c">{len(items)} listing(s) need immediate attention:</p>
    {cards}
    <div style="margin-top:20px;text-align:center">
      <a href="{sheet_url}" style="display:inline-block;background:#ff3b30;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">Open Sheet →</a>
    </div>
  </div>

  <p style="text-align:center;font-size:11px;color:#aeaeb2;margin-top:8px">Costco → eBay Agent · {run_time}</p>
</div>
</body></html>"""


def _html_routine(subject, summary_rows, product_rows, sheet_url, run_time):
    """
    Green summary template.
    summary_rows = list of (label, value, color) tuples for the stats table.
    product_rows = list of dicts with keys: title, row, note (optional detail lines).
    """
    table_rows = ""
    for i, (label, value, color) in enumerate(summary_rows):
        bg = "background:#f2f2f7;" if i % 2 == 0 else ""
        table_rows += f'<tr style="{bg}"><td style="padding:9px 12px;font-size:14px;color:#1d1d1f">{label}</td><td style="padding:9px 12px;text-align:right;font-weight:600;font-size:14px;color:{color}">{value}</td></tr>'

    detail = ""
    for p in product_rows:
        note = f'<br><span style="font-size:12px;color:#636366">{p["note"]}</span>' if p.get("note") else ""
        detail += f'<div style="padding:10px 0;border-bottom:1px solid #f2f2f7"><b style="font-size:14px">{p["title"]}</b> <span style="color:#aeaeb2;font-size:12px">Row {p["row"]}</span>{note}</div>'

    detail_block = f'<div style="margin-top:16px">{detail}</div>' if detail else ""

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:16px;background:#f2f2f7;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif">
<div style="max-width:600px;margin:0 auto">

  <div style="background:#34c759;padding:16px 24px;border-radius:10px 10px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">✓ {subject}</span>
    <span style="color:rgba(255,255,255,0.75);font-size:12px;float:right;line-height:26px">{run_time}</span>
  </div>

  <div style="background:#fff;border:1px solid #e5e5ea;border-top:none;padding:16px 24px;border-radius:0 0 10px 10px">
    <table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden">
      {table_rows}
    </table>
    {detail_block}
    <div style="margin-top:20px;text-align:center">
      <a href="{sheet_url}" style="display:inline-block;background:#007aff;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">Open Sheet →</a>
    </div>
  </div>

  <p style="text-align:center;font-size:11px;color:#aeaeb2;margin-top:8px">Costco → eBay Agent · {run_time}</p>
</div>
</body></html>"""


# ── SMS helpers ───────────────────────────────────────────────────────────────

def _sms_urgent(items):
    """160-char friendly urgent SMS. Truncates if needed."""
    lines = [f"⚠ URGENT: {len(items)} listing(s) need action"]
    for item in items[:3]:  # cap at 3 items for SMS length
        title_short = item['title'][:30]
        lines.append(f"- {title_short}: {item['reason'][:40]}")
    return "\n".join(lines)


def _sms_ready(items):
    """Brief SMS for 'ready to list' notifications."""
    titles = ", ".join(i['title'][:20] for i in items[:2])
    suffix = f" +{len(items)-2} more" if len(items) > 2 else ""
    return f"📦 Ready to list: {titles}{suffix}\nRun ebay_export.py"


# ── Public API ────────────────────────────────────────────────────────────────

def send_alert(subject, body, urgent=False):
    """
    Legacy plain-text API — kept for backward compatibility with researcher.py.
    Wraps body in a minimal routine HTML template.
    """
    from datetime import datetime
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID','')}/edit"

    # Wrap plain text in pre-formatted block inside routine template
    summary_rows = []
    product_rows = [{"title": line, "row": ""} for line in body.splitlines() if line.strip()][:8]
    html_body = _html_routine(subject, summary_rows, product_rows, sheet_url, run_time)

    _send_email(subject, html_body, urgent=urgent, plain_fallback=body)

    if urgent:
        sms_text = f"{'URGENT — ' if urgent else ''}{subject[:130]}\nCheck sheet now."
        _send_sms(sms_text)


def send_urgent_alert(subject, items, run_time=None, sheet_url=None):
    """
    Sends a red URGENT HTML email + SMS.

    items: list of dicts — {title, row, category, reason, reprice_note (opt)}
    """
    from datetime import datetime
    run_time = run_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    sheet_url = sheet_url or f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID','')}/edit"

    html_body = _html_urgent(subject, items, sheet_url, run_time)
    _send_email(f"🚨 URGENT — {subject}", html_body, urgent=True)
    _send_sms(_sms_urgent(items))


def send_routine_alert(subject, summary_rows, product_rows=None, run_time=None, sheet_url=None):
    """
    Sends a green routine HTML email (no SMS).

    summary_rows: list of (label, value, color) tuples
    product_rows: optional list of {title, row, note} dicts
    """
    from datetime import datetime
    run_time = run_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    sheet_url = sheet_url or f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID','')}/edit"

    html_body = _html_routine(subject, summary_rows, product_rows or [], sheet_url, run_time)
    _send_email(subject, html_body, urgent=False)


def send_ready_to_list_alert(items, run_time=None, sheet_url=None):
    """
    Sends green 'ready to list' email. SMS only if 3+ items pending.

    items: list of {title, row, has_copy} dicts
    """
    from datetime import datetime
    run_time = run_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    sheet_url = sheet_url or f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID','')}/edit"

    subject = f"{len(items)} product(s) ready to list on eBay"
    summary_rows = [
        ("Products ready to export", len(items), "#007aff"),
        ("Next step", "python tools/ebay_export.py", "#34c759"),
    ]
    product_rows = [
        {"title": i["title"], "row": i["row"], "note": "✓ Copy ready" if i.get("has_copy") else "⏳ Copy generating"}
        for i in items
    ]
    html_body = _html_routine(subject, summary_rows, product_rows, sheet_url, run_time)
    _send_email(subject, html_body, urgent=False)

    if len(items) >= 3:
        _send_sms(_sms_ready(items))


def send_rotation_digest(rotation_by_category, run_date, sheet_url=None):
    """
    Weekly rotation digest — purple header, per-category cards with bottom performers.

    rotation_by_category: {cat_name: [candidate dicts from rotation_engine]}
      Each candidate: {title, row, perf_score, reason, suggested_replacement: {title, perf_score} or None}
    """
    sheet_url = sheet_url or f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID','')}/edit"

    category_sections = ""
    for cat_name, candidates in rotation_by_category.items():
        candidate_rows = ""
        for c in candidates:
            replacement = c.get("suggested_replacement")
            if replacement:
                swap = (
                    f'<span style="color:#636366;font-size:12px">'
                    f'Consider: &ldquo;{replacement["title"][:55]}&rdquo; '
                    f'(score: {replacement["perf_score"]})</span>'
                )
            else:
                swap = '<span style="color:#aeaeb2;font-size:12px">No PENDING/WATCH replacement yet</span>'

            candidate_rows += f"""
            <div style="padding:10px 0;border-bottom:1px solid #f2f2f7">
              <b style="font-size:14px;color:#1d1d1f">{c['title'][:60]}</b>
              <span style="float:right;background:#ff3b30;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600">{c['perf_score']}</span>
              <br><span style="font-size:12px;color:#ff9500">{c['reason']}</span><br>
              {swap}
            </div>"""

        category_sections += f"""
        <div style="margin:16px 0">
          <div style="background:#f2f2f7;padding:10px 16px;border-radius:8px 8px 0 0">
            <b style="font-size:15px;color:#1d1d1f">&#10022; {cat_name}</b>
            <span style="color:#636366;font-size:12px;float:right">{len(candidates)} candidate(s)</span>
          </div>
          <div style="background:#fff;border:1px solid #e5e5ea;border-top:none;padding:0 16px;border-radius:0 0 8px 8px">
            {candidate_rows}
          </div>
        </div>"""

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:16px;background:#f2f2f7;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif">
<div style="max-width:600px;margin:0 auto">

  <div style="background:#5856d6;padding:16px 24px;border-radius:10px 10px 0 0">
    <span style="color:#fff;font-size:18px;font-weight:700">&#128260; Weekly Rotation Digest</span>
    <span style="color:rgba(255,255,255,0.75);font-size:12px;float:right;line-height:26px">{run_date}</span>
  </div>

  <div style="background:#fff;border:1px solid #e5e5ea;border-top:none;padding:16px 24px;border-radius:0 0 10px 10px">
    <p style="margin:0 0 12px;font-size:14px;color:#3a3a3c">
      These products are scoring below their category rotation threshold. Consider swapping
      underperformers for higher-potential PENDING/WATCH products to keep inventory fresh.
    </p>
    {category_sections}
    <div style="margin-top:20px;text-align:center">
      <a href="{sheet_url}" style="display:inline-block;background:#5856d6;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">Review in Sheet &#8594;</a>
    </div>
  </div>

  <p style="text-align:center;font-size:11px;color:#aeaeb2;margin-top:8px">Costco &#8594; eBay Agent &middot; Rotation Digest &middot; {run_date}</p>
</div>
</body></html>"""

    subject = f"Rotation Digest — {len(rotation_by_category)} category/categories flagged"
    _send_email(subject, html_body, urgent=False)


# ── Internal send helpers ─────────────────────────────────────────────────────

def _send_email(subject, html_body, urgent=False, plain_fallback=""):
    from_email = os.getenv("ALERT_FROM_EMAIL")
    from_password = os.getenv("ALERT_FROM_PASSWORD")
    to_email = os.getenv("ALERT_TO_EMAIL")

    if not all([from_email, from_password, to_email]):
        logger.warning("Email alert skipped — missing ALERT_* env vars")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject

        if plain_fallback:
            msg.attach(MIMEText(plain_fallback, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_email, from_password)
            server.sendmail(from_email, to_email, msg.as_string())

        logger.info(f"Email sent: {subject}")
    except Exception as e:
        logger.error(f"Email failed: {e}")


def _send_sms(text):
    from_email = os.getenv("ALERT_FROM_EMAIL")
    from_password = os.getenv("ALERT_FROM_PASSWORD")
    to_sms = os.getenv("ALERT_TO_SMS")

    if not all([from_email, from_password, to_sms]):
        return

    try:
        sms_msg = MIMEText(text[:160])
        sms_msg["From"] = from_email
        sms_msg["To"] = to_sms
        sms_msg["Subject"] = ""

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_email, from_password)
            server.sendmail(from_email, to_sms, sms_msg.as_string())

        logger.info("SMS sent")
    except Exception as e:
        logger.error(f"SMS failed: {e}")
