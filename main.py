"""
Swan AI Clone - Production Backend with RB2B Integration
Deploy to Railway.app
"""

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx
import json
import sqlite3
import os
from datetime import datetime
import asyncio

app = FastAPI(title="Swan AI Clone API", version="2.1.0")

# CORS - Allow all origins for tracking
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database path
DB_PATH = os.getenv("DB_PATH", "swan.db")

# API Keys from environment variables
def get_settings():
    return {
        "apollo_api_key": os.getenv("APOLLO_API_KEY", ""),
        "hunter_api_key": os.getenv("HUNTER_API_KEY", ""),
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "ipinfo_token": os.getenv("IPINFO_TOKEN", ""),
        "slack_webhook_url": os.getenv("SLACK_WEBHOOK_URL", ""),
        "icp_config": {
            "industries": ["SaaS", "Technology", "E-commerce", "Software", "Marketing", "Digital Agency"],
            "min_employees": 10,
            "max_employees": 5000,
            "countries": ["United States", "United Kingdom", "Canada", "India", "Australia", "Germany"],
            "target_titles": ["CEO", "CTO", "VP", "Director", "Head of", "Manager", "Founder"]
        }
    }

# ============== MODELS ==============

class VisitorData(BaseModel):
    project_id: str
    session_id: str
    ip_address: Optional[str] = None
    current_url: str
    referrer: Optional[str] = ""
    pages_viewed: List[Dict[str, Any]] = []
    visit_duration: int = 0
    user_agent: Optional[str] = ""
    screen_size: Optional[str] = ""
    timestamp: str
    event: Optional[str] = "pageview"

# ============== DATABASE ==============

def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS visitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            ip_address TEXT,
            pages_viewed TEXT,
            visit_duration INTEGER DEFAULT 0,
            referrer TEXT,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE,
            name TEXT,
            industry TEXT,
            employee_count INTEGER DEFAULT 0,
            country TEXT,
            city TEXT,
            description TEXT,
            funding_stage TEXT,
            total_funding INTEGER DEFAULT 0,
            annual_revenue INTEGER DEFAULT 0,
            linkedin_url TEXT,
            enriched_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            name TEXT,
            email TEXT,
            title TEXT,
            seniority TEXT,
            department TEXT,
            linkedin_url TEXT,
            confidence INTEGER DEFAULT 0,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT UNIQUE,
            company_id INTEGER,
            session_id TEXT,
            ip_address TEXT,
            identified_company TEXT,
            pages_viewed TEXT,
            visit_duration INTEGER DEFAULT 0,
            referrer TEXT,
            icp_score INTEGER DEFAULT 0,
            tier TEXT DEFAULT 'cold',
            match_reasons TEXT,
            intent_signals TEXT,
            research_summary TEXT,
            talking_points TEXT,
            email_draft TEXT,
            recommended_action TEXT,
            urgency TEXT DEFAULT 'low',
            status TEXT DEFAULT 'new',
            source TEXT DEFAULT 'tracking',
            person_name TEXT,
            person_email TEXT,
            person_title TEXT,
            person_linkedin TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
    """)
    
    # Add missing columns to existing database (for upgrades)
    try:
        cursor.execute("ALTER TABLE leads ADD COLUMN source TEXT DEFAULT 'tracking'")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE leads ADD COLUMN person_name TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE leads ADD COLUMN person_email TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE leads ADD COLUMN person_title TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE leads ADD COLUMN person_linkedin TEXT")
    except:
        pass
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

# ============== IP LOOKUP ==============

async def lookup_company_from_ip(ip_address: str, token: str = "") -> Dict:
    """Look up company from IP using IPinfo.io"""
    if not ip_address or ip_address in ["127.0.0.1", "localhost", "::1", ""]:
        return {"success": False, "error": "Local/missing IP"}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            url = f"https://ipinfo.io/{ip_address}"
            if token:
                url += f"?token={token}"
            
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                org = data.get("org", "")
                company_name = ""
                
                if org:
                    parts = org.split(" ", 1)
                    company_name = parts[1] if len(parts) > 1 else org
                
                # Try to guess domain
                domain = ""
                if company_name:
                    clean = company_name.lower().replace(" ", "").replace(",", "").replace(".", "")
                    for suffix in ["inc", "llc", "ltd", "corp", "corporation", "pvt", "private", "limited"]:
                        clean = clean.replace(suffix, "")
                    if len(clean) > 2:
                        domain = f"{clean[:20]}.com"
                
                return {
                    "success": True,
                    "data": {
                        "ip": ip_address,
                        "company_name": company_name,
                        "domain": domain,
                        "city": data.get("city", ""),
                        "region": data.get("region", ""),
                        "country": data.get("country", ""),
                        "org": org
                    }
                }
            return {"success": False, "error": f"Status {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ============== ENRICHMENT APIs ==============

async def enrich_company_apollo(domain: str, api_key: str) -> Dict:
    """Enrich company using Apollo API"""
    if not api_key or not domain:
        return {"success": False, "error": "Missing API key or domain"}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.post(
                "https://api.apollo.io/v1/organizations/enrich",
                headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
                json={"api_key": api_key, "domain": domain}
            )
            
            if response.status_code == 200:
                data = response.json()
                org = data.get("organization", {})
                if not org:
                    return {"success": False, "error": "No data found"}
                
                return {
                    "success": True,
                    "data": {
                        "name": org.get("name", ""),
                        "domain": org.get("primary_domain", domain),
                        "industry": org.get("industry", "Unknown"),
                        "employee_count": org.get("estimated_num_employees", 0),
                        "country": org.get("country", ""),
                        "city": org.get("city", ""),
                        "description": org.get("short_description", ""),
                        "funding_stage": org.get("latest_funding_stage", ""),
                        "total_funding": org.get("total_funding", 0),
                        "annual_revenue": org.get("annual_revenue", 0),
                        "linkedin_url": org.get("linkedin_url", ""),
                    }
                }
            return {"success": False, "error": response.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

async def find_contacts_hunter(domain: str, api_key: str) -> Dict:
    """Find contacts using Hunter.io"""
    if not api_key or not domain:
        return {"success": False, "contacts": []}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "limit": 5, "api_key": api_key}
            )
            
            if response.status_code == 200:
                data = response.json()
                emails = data.get("data", {}).get("emails", [])
                contacts = [{
                    "name": f"{e.get('first_name', '')} {e.get('last_name', '')}".strip(),
                    "email": e.get("value", ""),
                    "title": e.get("position", ""),
                    "department": e.get("department", ""),
                    "seniority": e.get("seniority", ""),
                    "linkedin_url": e.get("linkedin", ""),
                    "confidence": e.get("confidence", 0)
                } for e in emails[:5]]
                return {"success": True, "contacts": contacts}
            return {"success": False, "contacts": []}
        except Exception as e:
            return {"success": False, "contacts": [], "error": str(e)}

async def score_lead_openai(company: Dict, contacts: List, visit_data: Dict, person_data: Dict, api_key: str, icp_config: Dict) -> Dict:
    """Score lead using OpenAI"""
    if not api_key:
        return {"success": False, "error": "No OpenAI key"}
    
    prompt = f"""You are a B2B lead scoring AI. Score this website visitor.

ICP CRITERIA:
- Industries: {', '.join(icp_config.get('industries', []))}
- Company Size: {icp_config.get('min_employees', 10)} - {icp_config.get('max_employees', 5000)} employees
- Countries: {', '.join(icp_config.get('countries', []))}
- Target Titles: {', '.join(icp_config.get('target_titles', []))}

VISITOR'S COMPANY:
- Name: {company.get('name', 'Unknown')}
- Industry: {company.get('industry', 'Unknown')}
- Employees: {company.get('employee_count', 0)}
- Country: {company.get('country', 'Unknown')}
- Description: {company.get('description', '')[:200]}

PERSON (if identified):
- Name: {person_data.get('name', 'Unknown')}
- Title: {person_data.get('title', 'Unknown')}
- Email: {person_data.get('email', 'Unknown')}

BEHAVIOR ON SITE:
- Pages: {json.dumps([p.get('url', p) if isinstance(p, dict) else p for p in visit_data.get('pages_viewed', [])])}
- Duration: {visit_data.get('visit_duration', 0)} seconds
- Referrer: {visit_data.get('referrer', 'Direct')}

CONTACTS FOUND: {len(contacts)}

SCORING:
- Person identified with email = +25 points
- Target title match = +20 points
- HIGH INTENT pages: /pricing, /demo, /contact, /book = +15 points each
- MEDIUM INTENT: /case-studies, /services, /solutions = +5 points each
- Duration > 60s = +5, > 180s = +10
- ICP industry match = +20
- ICP size match = +15
- ICP country match = +10

OUTPUT JSON ONLY (no markdown):
{{
  "icp_score": <0-100>,
  "tier": "hot" | "warm" | "cold",
  "match_reasons": ["reason1", "reason2"],
  "intent_signals": ["signal1", "signal2"],
  "recommended_action": "book_demo" | "send_email" | "nurture" | "skip",
  "urgency": "high" | "medium" | "low",
  "research_summary": "2-3 sentences about this prospect",
  "talking_points": ["point1", "point2"],
  "email_draft": {{"subject": "...", "body": "3-4 sentences"}}
}}"""

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": "You are a B2B lead scoring AI. Respond with valid JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1000
                }
            )
            
            if response.status_code == 200:
                content = response.json()["choices"][0]["message"]["content"]
                content = content.replace("```json", "").replace("```", "").strip()
                return {"success": True, "data": json.loads(content)}
            return {"success": False, "error": response.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ============== VISITOR PROCESSING ==============

async def process_visitor(visitor: VisitorData, client_ip: str):
    """Main processing pipeline for tracking script"""
    settings = get_settings()
    ip = visitor.ip_address or client_ip or ""
    
    print(f"📍 Processing: IP={ip}, Session={visitor.session_id}")
    
    # Save raw visitor
    save_visitor(visitor, ip)
    
    # Skip local IPs
    if not ip or ip in ["127.0.0.1", "::1", "localhost"]:
        print("⚠️ Local IP, skipping enrichment")
        return
    
    lead_id = f"lead_{int(datetime.now().timestamp())}_{visitor.session_id[:8]}"
    
    # Step 1: IP Lookup
    ip_result = await lookup_company_from_ip(ip, settings.get("ipinfo_token", ""))
    identified = ip_result.get("data", {}).get("company_name", "") if ip_result.get("success") else ""
    domain = ip_result.get("data", {}).get("domain", "") if ip_result.get("success") else ""
    
    print(f"🔍 IP Lookup: {identified} ({domain})")
    
    # Step 2: Enrich with Apollo
    company = {"name": identified or "Unknown", "domain": domain, "industry": "Unknown", "employee_count": 0, "country": ip_result.get("data", {}).get("country", "")}
    if domain and settings.get("apollo_api_key"):
        result = await enrich_company_apollo(domain, settings["apollo_api_key"])
        if result.get("success"):
            company = result["data"]
            print(f"✅ Apollo: {company.get('name')} - {company.get('industry')}")
    
    # Step 3: Find contacts
    contacts = []
    if domain and settings.get("hunter_api_key"):
        result = await find_contacts_hunter(domain, settings["hunter_api_key"])
        if result.get("success"):
            contacts = result["contacts"]
            print(f"✅ Hunter: {len(contacts)} contacts")
    
    # Step 4: AI Scoring
    scoring = {
        "icp_score": 30, "tier": "cold", "match_reasons": [], 
        "intent_signals": [], "recommended_action": "skip", "urgency": "low",
        "research_summary": f"Visitor from {identified or ip}", "talking_points": [], "email_draft": {}
    }
    
    if settings.get("openai_api_key") and identified:
        visit_data = {"pages_viewed": visitor.pages_viewed, "visit_duration": visitor.visit_duration, "referrer": visitor.referrer}
        person_data = {}
        result = await score_lead_openai(company, contacts, visit_data, person_data, settings["openai_api_key"], settings["icp_config"])
        if result.get("success"):
            scoring = result["data"]
            print(f"✅ Score: {scoring.get('icp_score')}/100 ({scoring.get('tier')})")
    
    # Step 5: Save to database
    save_lead(lead_id, company, contacts, visitor, scoring, ip, identified, "tracking", {})
    
    # Step 6: Slack notification for hot leads
    if settings.get("slack_webhook_url") and scoring.get("tier") == "hot":
        await send_slack_alert(company, scoring, ip, {}, settings["slack_webhook_url"])
    
    print(f"✅ Lead saved: {lead_id}")

# ============== RB2B PROCESSING ==============

async def process_rb2b_lead(rb2b_data: dict):
    """Process lead from RB2B webhook - Person-level identification!"""
    settings = get_settings()
    
    # Extract person data from RB2B
    email = rb2b_data.get("Email", "") or rb2b_data.get("email", "")
    first_name = rb2b_data.get("First Name", "") or rb2b_data.get("first_name", "")
    last_name = rb2b_data.get("Last Name", "") or rb2b_data.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip()
    title = rb2b_data.get("Title", "") or rb2b_data.get("title", "")
    linkedin = rb2b_data.get("LinkedIn URL", "") or rb2b_data.get("linkedin_url", "")
    company_name = rb2b_data.get("Company", "") or rb2b_data.get("company", "")
    
    if not email:
        print("❌ RB2B: No email in data")
        return None
    
    # Extract domain from email
    domain = email.split("@")[-1] if "@" in email else ""
    
    print(f"🎯 RB2B Lead: {full_name} ({email}) - {title} at {company_name}")
    
    lead_id = f"rb2b_{int(datetime.now().timestamp())}_{email.split('@')[0][:8]}"
    
    # Enrich company with Apollo
    company = {"name": company_name or domain, "domain": domain, "industry": "Unknown", "employee_count": 0, "country": ""}
    if domain and settings.get("apollo_api_key"):
        result = await enrich_company_apollo(domain, settings["apollo_api_key"])
        if result.get("success"):
            company = result["data"]
            print(f"✅ Apollo: {company.get('name')} - {company.get('industry')}")
    
    # Find more contacts
    contacts = []
    if domain and settings.get("hunter_api_key"):
        result = await find_contacts_hunter(domain, settings["hunter_api_key"])
        if result.get("success"):
            contacts = result["contacts"]
            print(f"✅ Hunter: {len(contacts)} more contacts")
    
    # Person data for scoring
    person_data = {
        "name": full_name,
        "email": email,
        "title": title,
        "linkedin": linkedin
    }
    
    # AI Scoring - RB2B leads start with higher base score!
    scoring = {
        "icp_score": 60, "tier": "warm", "match_reasons": ["Person identified via RB2B"], 
        "intent_signals": ["Visited website"], "recommended_action": "send_email", "urgency": "medium",
        "research_summary": f"{full_name} from {company.get('name', company_name)} visited the website.", 
        "talking_points": [f"Visited your website", f"Works as {title}"], 
        "email_draft": {"subject": f"Following up on your visit", "body": f"Hi {first_name}, I noticed you visited our website..."}
    }
    
    if settings.get("openai_api_key"):
        visit_data = {"pages_viewed": [], "visit_duration": 0, "referrer": ""}
        result = await score_lead_openai(company, contacts, visit_data, person_data, settings["openai_api_key"], settings["icp_config"])
        if result.get("success"):
            scoring = result["data"]
            # RB2B leads get bonus points for having email
            scoring["icp_score"] = min(100, scoring.get("icp_score", 50) + 15)
            if scoring["icp_score"] >= 70:
                scoring["tier"] = "hot"
            elif scoring["icp_score"] >= 50:
                scoring["tier"] = "warm"
            print(f"✅ Score: {scoring.get('icp_score')}/100 ({scoring.get('tier')})")
    
    # Save to database
    save_rb2b_lead(lead_id, company, contacts, scoring, person_data, rb2b_data)
    
    # Slack notification for hot leads
    if settings.get("slack_webhook_url") and scoring.get("tier") == "hot":
        await send_slack_alert(company, scoring, "", person_data, settings["slack_webhook_url"])
    
    print(f"🎉 RB2B Lead saved: {lead_id}")
    return {"lead_id": lead_id, "person": person_data, "company": company, "scoring": scoring}

def save_rb2b_lead(lead_id: str, company: Dict, contacts: List, scoring: Dict, person: Dict, raw_data: Dict):
    """Save RB2B lead to database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Save/update company
    company_id = None
    if company.get("domain"):
        cursor.execute("""
            INSERT OR REPLACE INTO companies (domain, name, industry, employee_count, country, city, description, funding_stage, total_funding, linkedin_url, enriched_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (company.get("domain"), company.get("name"), company.get("industry"), company.get("employee_count", 0),
              company.get("country"), company.get("city"), company.get("description"), company.get("funding_stage"),
              company.get("total_funding", 0), company.get("linkedin_url"), json.dumps(company)))
        cursor.execute("SELECT id FROM companies WHERE domain=?", (company["domain"],))
        row = cursor.fetchone()
        if row:
            company_id = row[0]
        
        # Save contacts
        for c in contacts:
            cursor.execute("""
                INSERT OR IGNORE INTO contacts (company_id, name, email, title, seniority, department, linkedin_url, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (company_id, c.get("name"), c.get("email"), c.get("title"), c.get("seniority"), c.get("department"), c.get("linkedin_url"), c.get("confidence", 0)))
    
    # Save lead with person data
    cursor.execute("""
        INSERT INTO leads (lead_id, company_id, session_id, ip_address, identified_company, pages_viewed, visit_duration, referrer, 
                          icp_score, tier, match_reasons, intent_signals, research_summary, talking_points, email_draft, 
                          recommended_action, urgency, source, person_name, person_email, person_title, person_linkedin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (lead_id, company_id, "", "", company.get("name", ""), json.dumps([]), 0, "",
          scoring.get("icp_score", 0), scoring.get("tier", "warm"), json.dumps(scoring.get("match_reasons", [])),
          json.dumps(scoring.get("intent_signals", [])), scoring.get("research_summary", ""), json.dumps(scoring.get("talking_points", [])),
          json.dumps(scoring.get("email_draft", {})), scoring.get("recommended_action", "send_email"), scoring.get("urgency", "medium"),
          "rb2b", person.get("name", ""), person.get("email", ""), person.get("title", ""), person.get("linkedin", "")))
    
    conn.commit()
    conn.close()

def save_visitor(visitor: VisitorData, ip: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO visitors (session_id, ip_address, pages_viewed, visit_duration, referrer, user_agent)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (visitor.session_id, ip, json.dumps(visitor.pages_viewed), visitor.visit_duration, visitor.referrer, visitor.user_agent))
    conn.commit()
    conn.close()

def save_lead(lead_id: str, company: Dict, contacts: List, visitor: VisitorData, scoring: Dict, ip: str, identified: str, source: str, person: Dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Save company
    company_id = None
    if company.get("domain"):
        cursor.execute("""
            INSERT OR REPLACE INTO companies (domain, name, industry, employee_count, country, city, description, funding_stage, total_funding, linkedin_url, enriched_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (company.get("domain"), company.get("name"), company.get("industry"), company.get("employee_count", 0),
              company.get("country"), company.get("city"), company.get("description"), company.get("funding_stage"),
              company.get("total_funding", 0), company.get("linkedin_url"), json.dumps(company)))
        company_id = cursor.lastrowid or cursor.execute("SELECT id FROM companies WHERE domain=?", (company["domain"],)).fetchone()[0]
        
        # Save contacts
        for c in contacts:
            cursor.execute("""
                INSERT OR IGNORE INTO contacts (company_id, name, email, title, seniority, department, linkedin_url, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (company_id, c.get("name"), c.get("email"), c.get("title"), c.get("seniority"), c.get("department"), c.get("linkedin_url"), c.get("confidence", 0)))
    
    # Save lead
    cursor.execute("""
        INSERT INTO leads (lead_id, company_id, session_id, ip_address, identified_company, pages_viewed, visit_duration, referrer, 
                          icp_score, tier, match_reasons, intent_signals, research_summary, talking_points, email_draft, 
                          recommended_action, urgency, source, person_name, person_email, person_title, person_linkedin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (lead_id, company_id, visitor.session_id, ip, identified, json.dumps(visitor.pages_viewed), visitor.visit_duration, visitor.referrer,
          scoring.get("icp_score", 0), scoring.get("tier", "cold"), json.dumps(scoring.get("match_reasons", [])),
          json.dumps(scoring.get("intent_signals", [])), scoring.get("research_summary", ""), json.dumps(scoring.get("talking_points", [])),
          json.dumps(scoring.get("email_draft", {})), scoring.get("recommended_action", ""), scoring.get("urgency", "low"),
          source, person.get("name", ""), person.get("email", ""), person.get("title", ""), person.get("linkedin", "")))
    
    conn.commit()
    conn.close()

async def send_slack_alert(company: Dict, scoring: Dict, ip: str, person: Dict, webhook_url: str):
    """Send Slack notification for hot leads"""
    person_info = ""
    if person.get("name"):
        person_info = f"\n*Person:* {person.get('name')} - {person.get('title', 'N/A')}\n*Email:* {person.get('email', 'N/A')}"
    
    message = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"🔥 HOT LEAD: {company.get('name', 'Unknown')}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Score:* {scoring.get('icp_score', 0)}/100 | *Industry:* {company.get('industry', 'Unknown')}{person_info}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:* {scoring.get('research_summary', '')}"}}
        ]
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.post(webhook_url, json=message)
        except:
            pass

# ============== API ENDPOINTS ==============

@app.on_event("startup")
async def startup():
    init_db()

@app.get("/")
async def root():
    return {"message": "Swan AI Clone API with RB2B", "version": "2.1", "status": "running"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

# ============== RB2B WEBHOOK ==============

@app.post("/webhook/rb2b")
async def rb2b_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive visitor data from RB2B webhook"""
    try:
        body = await request.json()
        print(f"📡 RB2B Webhook received!")
        print(f"📦 Data: {json.dumps(body, indent=2)}")
        
        # Process in background
        background_tasks.add_task(process_rb2b_lead, body)
        
        return {"status": "received", "message": "Processing RB2B lead"}
    except Exception as e:
        print(f"❌ RB2B Webhook error: {e}")
        return {"status": "error", "message": str(e)}

# ============== TRACKING WEBHOOK ==============

@app.post("/webhook/visitor")
async def receive_visitor(request: Request, visitor: VisitorData, background_tasks: BackgroundTasks):
    """Receive tracking data from website"""
    # Get real IP
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.headers.get("X-Real-IP", "")
    if not client_ip:
        client_ip = request.client.host if request.client else ""
    
    print(f"🌐 Webhook: IP={client_ip}, Session={visitor.session_id}, Event={visitor.event}")
    
    # Process in background
    background_tasks.add_task(process_visitor, visitor, client_ip)
    
    return {"status": "received", "ip": client_ip}

@app.post("/api/test-rb2b")
async def test_rb2b():
    """Test RB2B processing with sample data"""
    sample_data = {
        "First Name": "John",
        "Last Name": "Smith",
        "Email": "john.smith@shopify.com",
        "Title": "VP of Marketing",
        "Company": "Shopify",
        "LinkedIn URL": "https://www.linkedin.com/in/johnsmith/"
    }
    result = await process_rb2b_lead(sample_data)
    return {"status": "success", "result": result}

@app.post("/api/test-visitor")
async def test_visitor(domain: str = "notion.so"):
    """Test with a specific domain"""
    settings = get_settings()
    lead_id = f"test_{int(datetime.now().timestamp())}"
    
    # Enrich
    company = {"name": domain.split(".")[0].title(), "domain": domain, "industry": "Unknown", "employee_count": 0}
    if settings.get("apollo_api_key"):
        result = await enrich_company_apollo(domain, settings["apollo_api_key"])
        if result.get("success"):
            company = result["data"]
    
    # Contacts
    contacts = []
    if settings.get("hunter_api_key"):
        result = await find_contacts_hunter(domain, settings["hunter_api_key"])
        contacts = result.get("contacts", [])
    
    # Score
    scoring = {"icp_score": 50, "tier": "warm", "match_reasons": [], "intent_signals": [], "research_summary": "Test lead", "talking_points": [], "email_draft": {}}
    if settings.get("openai_api_key"):
        visit_data = {"pages_viewed": ["/", "/pricing"], "visit_duration": 120, "referrer": ""}
        result = await score_lead_openai(company, contacts, visit_data, {}, settings["openai_api_key"], settings["icp_config"])
        if result.get("success"):
            scoring = result["data"]
    
    return {"lead_id": lead_id, "company": company, "contacts": contacts, "scoring": scoring}

@app.get("/api/leads")
async def get_leads(limit: int = 50, tier: Optional[str] = None, source: Optional[str] = None):
    """Get all leads"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = """
        SELECT l.*, c.name as company_name, c.domain, c.industry, c.employee_count, c.country, c.funding_stage
        FROM leads l LEFT JOIN companies c ON l.company_id = c.id WHERE 1=1
    """
    params = []
    if tier:
        query += " AND l.tier = ?"
        params.append(tier)
    if source:
        query += " AND l.source = ?"
        params.append(source)
    query += " ORDER BY l.created_at DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    leads = []
    for row in cursor.fetchall():
        lead = dict(row)
        for f in ['pages_viewed', 'match_reasons', 'intent_signals', 'talking_points', 'email_draft']:
            if lead.get(f):
                try:
                    lead[f] = json.loads(lead[f])
                except:
                    pass
        leads.append(lead)
    
    conn.close()
    return {"leads": leads, "total": len(leads)}

@app.get("/api/leads/{lead_id}")
async def get_lead(lead_id: str):
    """Get single lead with contacts"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT l.*, c.name as company_name, c.domain, c.industry, c.employee_count, c.country, c.city, c.description, c.funding_stage, c.total_funding, c.linkedin_url
        FROM leads l LEFT JOIN companies c ON l.company_id = c.id WHERE l.lead_id = ?
    """, (lead_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(404, "Lead not found")
    
    lead = dict(row)
    for f in ['pages_viewed', 'match_reasons', 'intent_signals', 'talking_points', 'email_draft']:
        if lead.get(f):
            try:
                lead[f] = json.loads(lead[f])
            except:
                pass
    
    cursor.execute("SELECT * FROM contacts WHERE company_id = ?", (lead.get("company_id"),))
    lead["contacts"] = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    return lead

@app.delete("/api/leads/{lead_id}")
async def delete_lead(lead_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute("DELETE FROM leads WHERE lead_id = ?", (lead_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

@app.get("/api/stats")
async def get_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    stats = {}
    cursor.execute("SELECT COUNT(*) FROM leads")
    stats["total_leads"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM leads WHERE tier='hot'")
    stats["hot_leads"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM leads WHERE tier='warm'")
    stats["warm_leads"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM leads WHERE tier='cold'")
    stats["cold_leads"] = cursor.fetchone()[0]
    cursor.execute("SELECT AVG(icp_score) FROM leads")
    stats["avg_score"] = round(cursor.fetchone()[0] or 0, 1)
    cursor.execute("SELECT COUNT(*) FROM visitors")
    stats["total_visits"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM leads WHERE source='rb2b'")
    stats["rb2b_leads"] = cursor.fetchone()[0]
    
    conn.close()
    return stats

@app.get("/api/visitors")
async def get_visitors(limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM visitors ORDER BY created_at DESC LIMIT ?", (limit,))
    visitors = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return {"visitors": visitors}

# Simple dashboard redirect
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Swan AI - Dashboard</title>
        <meta http-equiv="refresh" content="0;url=https://swanclone1-git-main-dlmavs-projects.vercel.app">
    </head>
    <body>
        <p>Redirecting to dashboard...</p>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
