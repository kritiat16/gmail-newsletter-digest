import os
import sys
import time
import re
import json
import html
import datetime
import imaplib
import email
from email.utils import parsedate_to_datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import argparse
import logging
import requests
from bs4 import BeautifulSoup

# Setup logging
script_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(script_dir, 'digest.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def load_env(env_path):
    """Manually load .env file variables to avoid extra dependency."""
    if not os.path.exists(env_path):
        logging.warning(f"No .env file found at {env_path}")
        return
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip()

def clean_html_and_extract_links(html_content):
    """Parse HTML to get cleaned text body and list of key hyperlinks."""
    if not html_content:
        return "", []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
            
        # Get all links
        links = []
        for a in soup.find_all('a', href=True):
            link_text = a.get_text().strip()
            link_url = a['href']
            if link_url.startswith('http') and link_text:
                low_text = link_text.lower()
                low_url = link_url.lower()
                # Exclude obvious navigation/social/unsubscribe links
                is_noise = any(noise in low_text or noise in low_url for noise in [
                    'unsubscribe', 'opt-out', 'preferences', 'view in browser', 
                    'privacy-policy', 'terms-of-service', 'twitter.com', 'facebook.com', 
                    'linkedin.com', 'instagram.com', 'youtube.com', 'pinterest.com',
                    'github.com', 'contact-us', 'about-us', 'help-center', 'manage-subscription'
                ])
                if not is_noise:
                    links.append({"text": link_text[:60], "url": link_url})
                    
        # Get text
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = '\n'.join(chunk for chunk in chunks if chunk)
        
        return clean_text, links
    except Exception as e:
        logging.error(f"Error parsing HTML: {e}")
        return "", []

def extract_links_from_text(text):
    """Fallback link extraction for plain text emails."""
    urls = re.findall(r'(https?://\S+)', text)
    links = []
    for url in urls:
        # Strip trailing punctuation often caught in regex
        url = url.rstrip('.,);]')
        low_url = url.lower()
        is_noise = any(noise in low_url for noise in [
            'unsubscribe', 'opt-out', 'preferences', 'privacy', 'terms', 'twitter.com',
            'facebook.com', 'linkedin.com', 'instagram.com', 'youtube.com'
        ])
        if not is_noise:
            links.append({"text": "Link", "url": url})
    return links

def get_email_body(msg):
    """Walk email parts to extract text and html body."""
    body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if "attachment" not in content_disposition:
                if content_type == "text/plain":
                    try:
                        body += part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
                    except Exception:
                        pass
                elif content_type == "text/html":
                    try:
                        html_body += part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
                    except Exception:
                        pass
    else:
        content_type = msg.get_content_type()
        if content_type == "text/plain":
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='ignore')
        elif content_type == "text/html":
            html_body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='ignore')
            
    return body, html_body

def is_newsletter(msg, body_text, html_body):
    """Filter to determine if an email is likely a newsletter."""
    # 1. Check for standard bulk/list email headers
    if msg.get('List-Unsubscribe') or msg.get('List-Id'):
        return True
    
    precedence = msg.get('Precedence', '').lower()
    if 'bulk' in precedence or 'list' in precedence:
        return True
        
    # 2. Check content for common newsletter keywords
    body_to_check = (body_text or "") + (html_body or "")
    if body_to_check:
        low_body = body_to_check.lower()
        if 'unsubscribe' in low_body:
            return True
        if 'view in browser' in low_body or 'view this email in your browser' in low_body:
            return True
            
    # 3. Check subject/sender for cues
    subject = msg.get('Subject', '').lower()
    sender = msg.get('From', '').lower()
    for keyword in ['newsletter', 'digest', 'weekly', 'daily', 'bulletin', 'dispatch', 'briefing']:
        if keyword in subject or keyword in sender:
            return True
            
    return False

def fetch_recent_newsletters(username, password):
    """Fetch emails from the last 24 hours and filter for newsletters."""
    logging.info("Connecting to Gmail IMAP server...")
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(username, password)
    mail.select('inbox')

    # Search for emails from the last 2 days to account for timezone boundary differences
    two_days_ago = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%d-%b-%Y")
    status, data = mail.search(None, f'(SINCE "{two_days_ago}")')
    
    if status != 'OK':
        logging.error("Failed to search emails.")
        return []

    mail_ids = data[0].split()
    logging.info(f"Found {len(mail_ids)} total emails in the last 48 hours. Filtering for newsletters in the last 24 hours...")

    newsletters = []
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=24)

    for mail_id in mail_ids:
        try:
            status, msg_data = mail.fetch(mail_id, '(RFC822)')
            if status != 'OK':
                continue
            
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            # Filter by date/time (strict 24 hours)
            date_str = msg.get('Date')
            if not date_str:
                continue
            try:
                email_date = parsedate_to_datetime(date_str)
            except Exception:
                logging.warning(f"Could not parse email date: {date_str}")
                continue
                
            if email_date < cutoff:
                continue # Outside 24 hour window

            subject = msg.get('Subject', '(No Subject)')
            # Decode email subject safely
            decoded_subject = ""
            for part, encoding in email.header.decode_header(subject):
                if isinstance(part, bytes):
                    decoded_subject += part.decode(encoding or 'utf-8', errors='ignore')
                else:
                    decoded_subject += part

            sender = msg.get('From', '(Unknown Sender)')
            decoded_sender = ""
            for part, encoding in email.header.decode_header(sender):
                if isinstance(part, bytes):
                    decoded_sender += part.decode(encoding or 'utf-8', errors='ignore')
                else:
                    decoded_sender += part

            body, html_body = get_email_body(msg)
            
            if is_newsletter(msg, body, html_body):
                # Clean html / extract links
                if html_body:
                    clean_text, links = clean_html_and_extract_links(html_body)
                else:
                    clean_text = body
                    links = extract_links_from_text(body)
                
                newsletters.append({
                    "subject": decoded_subject,
                    "sender": decoded_sender,
                    "date": email_date.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "text": clean_text,
                    "links": links
                })
        except Exception as e:
            logging.error(f"Error processing email ID {mail_id}: {e}")

    mail.close()
    mail.logout()
    logging.info(f"Successfully identified {len(newsletters)} newsletter emails from the last 24 hours.")
    return newsletters

def process_batch(newsletters, api_key):
    """Process a small batch of newsletters with Gemini API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    prompt = (
        "You are an expert editor. You have been given a list of newsletter emails received in the last 24 hours.\n"
        "Your task is to review each newsletter and:\n"
        "1. Write a 1-2 line concise summary of its main points.\n"
        "2. Select up to 3 most important key links from the provided links. Do NOT include unsubscribe, preferences, social media, or login links.\n"
        "3. Rate its importance: 'HIGH' (breaking news, launches, time-sensitive) or 'LOW' (evergreen, general reads, can wait).\n"
        "4. Assign it to one of these categories: 'Tech & AI', 'Business & Finance', 'Health & Lifestyle', or 'Other'.\n\n"
        "If you determine an email is NOT actually a newsletter (e.g. personal email, verification code, invoice), do not include it in the output.\n\n"
        "Here are the emails:\n"
    )
    
    for idx, nl in enumerate(newsletters):
        truncated_text = nl['text'][:4000]
        truncated_links = nl['links'][:25]
        
        prompt += f"\n--- Email #{idx+1} ---\n"
        prompt += f"Sender: {nl['sender']}\n"
        prompt += f"Subject: {nl['subject']}\n"
        prompt += f"Date: {nl['date']}\n"
        prompt += f"Body Text:\n{truncated_text}\n"
        prompt += f"Available Links:\n{json.dumps(truncated_links)}\n"
        prompt += "---------------------\n"
        
    schema = {
        "type": "OBJECT",
        "properties": {
            "newsletters": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "subject": {"type": "STRING"},
                        "sender": {"type": "STRING"},
                        "summary": {"type": "STRING"},
                        "key_links": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "title": {"type": "STRING"},
                                    "url": {"type": "STRING"}
                                },
                                "required": ["title", "url"]
                            }
                        },
                        "importance": {"type": "STRING", "enum": ["HIGH", "LOW"]},
                        "category": {"type": "STRING", "enum": ["Tech & AI", "Business & Finance", "Health & Lifestyle", "Other"]}
                    },
                    "required": ["subject", "sender", "summary", "key_links", "importance", "category"]
                }
            }
        },
        "required": ["newsletters"]
    }
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema
        }
    }
    
    headers = {"Content-Type": "application/json"}
    
    max_retries = 3
    backoff = 6
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=45)
            
            # Handle rate limits or temporary server errors with backoff
            if response.status_code in [429, 503]:
                logging.warning(f"Gemini API returned status {response.status_code}. Retrying in {backoff} seconds (attempt {attempt+1}/{max_retries})...")
                time.sleep(backoff)
                backoff *= 2
                continue
                
            response.raise_for_status()
            res_data = response.json()
            
            text_response = res_data['candidates'][0]['content']['parts'][0]['text']
            parsed_result = json.loads(text_response)
            return parsed_result.get("newsletters", [])
        except Exception as e:
            if attempt == max_retries - 1:
                logging.error(f"Error calling Gemini API for batch after {max_retries} attempts: {e}")
                if 'response' in locals() and response.text:
                    logging.error(f"API Response: {response.text}")
                return []
            logging.warning(f"Error calling Gemini API on attempt {attempt+1}/{max_retries}: {e}. Retrying in {backoff} seconds...")
            time.sleep(backoff)
            backoff *= 2
    return []

def process_newsletters_with_gemini(newsletters, api_key):
    """Send newsletters data to Gemini in batches to get summaries, links, importance, and categories."""
    if not newsletters:
        return []
        
    logging.info(f"Calling Gemini API to summarize and organize {len(newsletters)} newsletters in batches...")
    
    all_processed = []
    batch_size = 5
    
    for i in range(0, len(newsletters), batch_size):
        if i > 0:
            logging.info("Sleeping for 5 seconds between batches to avoid API rate limits...")
            time.sleep(5)
            
        batch = newsletters[i:i+batch_size]
        logging.info(f"Processing batch {i//batch_size + 1} of {(len(newsletters)-1)//batch_size + 1} ({len(batch)} newsletters)...")
        
        batch_processed = process_batch(batch, api_key)
        all_processed.extend(batch_processed)
        
    return all_processed

def generate_digest_html(processed_newsletters, date_str):
    """Generate a gorgeous HTML email digest using inline CSS."""
    
    # Categorize
    categories = {
        "Tech & AI": [],
        "Business & Finance": [],
        "Health & Lifestyle": [],
        "Other": []
    }
    
    for nl in processed_newsletters:
        cat = nl.get("category", "Other")
        if cat in categories:
            categories[cat].append(nl)
        else:
            categories["Other"].append(nl)
            
    # Config for sections
    styles = {
        "Tech & AI": {"color": "#0891b2", "border": "#06b6d4", "bg": "#ecfeff", "icon": "🤖"},
        "Business & Finance": {"color": "#16a34a", "border": "#22c55e", "bg": "#f0fdf4", "icon": "📈"},
        "Health & Lifestyle": {"color": "#9333ea", "border": "#a855f7", "bg": "#faf5ff", "icon": "🌱"},
        "Other": {"color": "#4b5563", "border": "#6b7280", "bg": "#f9fafb", "icon": "📨"}
    }
    
    content_html = ""
    
    if not processed_newsletters:
        content_html = """
        <div style="text-align: center; padding: 40px 20px;">
          <span style="font-size: 48px;">☕</span>
          <h3 style="margin: 16px 0 8px 0; color: #334155;">No newsletters found</h3>
          <p style="margin: 0; color: #64748b; font-size: 14px;">No newsletters were received in your inbox in the last 24 hours. Enjoy your quiet morning!</p>
        </div>
        """
    else:
        for cat_name, items in categories.items():
            if not items:
                continue
                
            cfg = styles[cat_name]
            content_html += f"""
            <div style="margin-bottom: 32px;">
              <h2 style="font-size: 16px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: {cfg['color']}; margin-top: 0; margin-bottom: 16px; border-bottom: 2px solid {cfg['border']}; padding-bottom: 6px;">
                <span style="margin-right: 8px;">{cfg['icon']}</span>{cat_name}
              </h2>
            """
            
            for item in items:
                # Importance badge
                imp = item.get("importance", "LOW")
                if imp == "HIGH":
                    badge_style = "background-color: #fee2e2; color: #991b1b; border: 1px solid #fecaca;"
                else:
                    badge_style = "background-color: #f3f4f6; color: #374151; border: 1px solid #e5e7eb;"
                    
                # Links list
                links_html = ""
                key_links = item.get("key_links", [])
                if key_links:
                    links_html += '<div style="margin-top: 12px; font-size: 13px; font-weight: 600; color: #475569;">Key Links:</div>'
                    links_html += '<ul style="margin: 4px 0 0 0; padding-left: 20px; color: #4f46e5; font-size: 13px;">'
                    for l in key_links:
                        l_title = html.escape(l.get("title", "Read link"))
                        l_url = l.get("url", "#")
                        links_html += f'<li style="margin-bottom: 4px;"><a href="{l_url}" target="_blank" style="color: #4f46e5; text-decoration: none; font-weight: 500;">{l_title} &rarr;</a></li>'
                    links_html += '</ul>'
                    
                content_html += f"""
                <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.02);">
                  <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <span style="font-size: 12px; color: #64748b; font-weight: 500;">{html.escape(item['sender'])}</span>
                    <span style="font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 2px 8px; border-radius: 9999px; {badge_style}">{imp}</span>
                  </div>
                  <h3 style="margin: 0 0 8px 0; font-size: 15px; font-weight: 600; color: #0f172a; line-height: 1.4;">{html.escape(item['subject'])}</h3>
                  <p style="margin: 0; font-size: 14px; color: #334155; line-height: 1.5;">{html.escape(item['summary'])}</p>
                  {links_html}
                </div>
                """
            content_html += "</div>"

    html_email = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Morning Newsletter Digest</title>
</head>
<body style="margin: 0; padding: 20px; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b;">
  <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.02); overflow: hidden; border: 1px solid #e2e8f0;">
    
    <!-- Header -->
    <div style="background: linear-gradient(135deg, #1e1b4b 0%, #312e81 100%); padding: 32px 24px; text-align: center; color: #ffffff;">
      <span style="font-size: 40px; display: block; margin-bottom: 8px;">🗞️</span>
      <h1 style="margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.025em;">Morning Newsletter Digest</h1>
      <p style="margin: 8px 0 0 0; font-size: 14px; color: #c7d2fe; font-weight: 500;">{date_str}</p>
    </div>
    
    <!-- Content Area -->
    <div style="padding: 24px; background-color: #f8fafc;">
      {content_html}
    </div>
    
    <!-- Footer -->
    <div style="background-color: #ffffff; padding: 20px 24px; text-align: center; border-top: 1px solid #e2e8f0;">
      <p style="margin: 0; font-size: 12px; color: #64748b; line-height: 1.5;">
        This email was automatically generated and sent to you by your Antigravity Scheduler.<br>
        Daily run scheduled at 7:30 AM IST (10:00 PM local).
      </p>
    </div>
    
  </div>
</body>
</html>
"""
    return html_email

def send_digest_email(html_content, username, password, date_str):
    """Send the HTML email digest via SMTP to user's own email address."""
    logging.info("Sending digest email via SMTP...")
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"🗞️ Your Morning Newsletter Digest — {date_str}"
    msg['From'] = username
    msg['To'] = username
    
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        # Connect to Gmail SMTP
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(username, password)
        server.sendmail(username, username, msg.as_string())
        server.close()
        logging.info("Digest email sent successfully!")
    except Exception as e:
        logging.error(f"Failed to send email via SMTP: {e}")
        raise

def main():
    parser = argparse.ArgumentParser(description="Gmail Morning Newsletter Digest")
    parser.add_argument('--dry-run', action='store_true', help="Fetch and process newsletters, save a preview.html file, but do not send the email.")
    args = parser.parse_args()

    # Load environment variables
    env_path = os.path.join(script_dir, '.env')
    load_env(env_path)

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pwd = os.getenv("GMAIL_APP_PASSWORD")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if not gmail_user or not gmail_pwd or not gemini_key:
        logging.error("Configuration missing in .env file. Please check GMAIL_USER, GMAIL_APP_PASSWORD, and GEMINI_API_KEY.")
        sys.exit(1)

    today_str = datetime.date.today().strftime("%B %d, %Y")

    try:
        # 1. Fetch recent newsletters
        newsletters = fetch_recent_newsletters(gmail_user, gmail_pwd)
        
        # 2. Process with Gemini
        processed = []
        if newsletters:
            processed = process_newsletters_with_gemini(newsletters, gemini_key)
            logging.info(f"Gemini returned {len(processed)} categorized newsletters.")
        else:
            logging.info("No newsletters to process with Gemini.")
            
        # 3. Generate HTML
        html_content = generate_digest_html(processed, today_str)
        
        # 4. Handle sending or dry-run preview
        if args.dry_run:
            preview_file = os.path.join(script_dir, 'preview.html')
            with open(preview_file, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logging.info(f"[Dry Run] Generated digest preview saved to {preview_file}")
        else:
            send_digest_email(html_content, gmail_user, gmail_pwd, today_str)
            
    except Exception as e:
        logging.exception(f"An unexpected error occurred during execution: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
