# Attendance Bot Architecture & Supabase Admin Dashboard Guide

This guide details:
1. **How the Attendance Bot works end-to-end** (syntax verification, state machine questionnaire, admin forwarding).
2. **How to integrate Supabase** so that every request submitted by users is automatically stored in your database and displayed live on your Admin Website.

---

## 1. End-to-End System Architecture

```
                                  ┌─────────────────────────────┐
                                  │   Google Sheets Service     │
                                  │ (Shared with Bot Service Acc)│
                                  └──────────────┬──────────────┘
                                                 │ Reads layout & marks
                                                 ▼
┌──────────────────┐    Sheet Link     ┌─────────────────────────────┐
│  Teacher / User  ├──────────────────►│    Telegram Bot (Render)    │
│  (Telegram Chat) │◄──────────────────┤ (python-telegram-bot v21)  │
└──────────────────┘  3-Step Question  └──────────────┬──────────────┘
                        (College,                     │
                        Branch, Subject)              │
                                                      │
                       ┌──────────────────────────────┴──────────────────────────────┐
                       │ Dual Dispatch upon Completion                               │
                       ▼                                                             ▼
         ┌─────────────────────────────┐                               ┌─────────────────────────────┐
         │     Telegram Admin Chat     │                               │      Supabase Database      │
         │ (Instant alerts to admin)   │                               │ (Table: integration_requests)│
         └─────────────────────────────┘                               └──────────────┬──────────────┘
                                                                                      │
                                                                                      │ Real-time WebSockets
                                                                                      ▼
                                                                       ┌─────────────────────────────┐
                                                                       │       Admin Website         │
                                                                       │  (React / Next.js / Vue)    │
                                                                       │  Manage & Approve Requests  │
                                                                       └─────────────────────────────┘
```

---

## 2. How the Bot Works (Deep Dive)

### Step 1: Link Detection & Sanitization
* The user sends a Google Sheets link (e.g. `https://docs.google.com/spreadsheets/d/1f2XM.../edit`) or Document ID.
* The bot uses a strict Regex filter:
  ```python
  SHEET_URL_REGEX = r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)"
  DOC_ID_REGEX    = r"^[a-zA-Z0-9-_]{30,60}$"
  ```
* Any non-link messages are ignored by the validator entry point, ensuring conversational text does not trigger false validations.

### Step 2: Google Sheets Fetch via Service Account
* Using `gspread`, the bot connects to Google Drive via service account credentials (`credentials.json` locally or `GOOGLE_CREDENTIALS_JSON` environment variable on Render).
* If the sheet is private, the bot immediately alerts the user with the exact service account email to share the sheet with.

### Step 3: Template Syntax Validation Engine (`validator.py`)
The validator inspects every worksheet for conformity:
1. **Dynamic Header Search (Rows 1–15):** Finds the row containing the 7 base columns:
   `['S.NO', 'Roll NO', 'Reg.NO', 'Student Name', 'Present', 'Absent', 'Percentage']`
   *(This accommodates Row 6 headers preceded by institution logos/titles).*
2. **Columns H+ (Attendance Dates):** Any header label is accepted (no strict date-parsing restrictions).
3. **Student Data Rows:**
   * Skips empty decorative/spacer rows (e.g. Row 7).
   * Validates `S.NO` as an incremental integer.
   * Ensures `Roll NO` and `Student Name` are populated.
   * Verifies attendance marks under columns H+ are strictly `'P'`, `'A'`, or blank/hyphen.
4. **Summary Feedback:** If invalid, the bot reports the top 5 exact errors with row/column locations. If valid, it returns the total student count and attendance session count.

### Step 4: 3-Step Interactive State Machine
Upon successful verification, the bot transitions into a `ConversationHandler`:
1. **Step 1:** Prompts user: `🏛️ Step 1 of 3: Please enter your College Name:`
2. **Step 2:** Prompts user: `🏢 Step 2 of 3: Please enter your Branch Name:`
3. **Step 3:** Prompts user: `📚 Step 3 of 3: Please enter Subject to be integrated:`

### Step 5: Dispatch to Admin Chat
When the subject is entered:
* It formats a comprehensive card containing the user's name, `@username`, Telegram `chat_id`, sheet link, student metrics, college, branch, and subject.
* Sends it directly to the Telegram `ADMIN_ID`.
* Returns a confirmation summary to the user.

---

## 3. Supabase Integration Setup

To mirror all integration requests onto your Admin Website, we store them in a Supabase PostgreSQL table.

### Step 3.1: Create the Supabase Table (SQL)
Run this query in your **Supabase Dashboard -> SQL Editor**:

```sql
-- 1. Create the integration_requests table
CREATE TABLE IF NOT EXISTS public.integration_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,

    -- User Information
    user_id BIGINT NOT NULL,              -- Telegram Chat ID
    user_name TEXT,                       -- Telegram Full Name
    user_handle TEXT,                     -- Telegram @username

    -- Academic Integration Details
    college_name TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    subject_name TEXT NOT NULL,

    -- Sheet Information
    spreadsheet_title TEXT,
    spreadsheet_url TEXT NOT NULL,
    document_id TEXT NOT NULL,
    tab_names TEXT[],                     -- List of worksheets validated
    student_count INTEGER DEFAULT 0,
    session_count INTEGER DEFAULT 0,

    -- Status Pipeline: pending -> under_review -> integrated -> rejected
    status TEXT DEFAULT 'pending' NOT NULL CHECK (status IN ('pending', 'under_review', 'integrated', 'rejected')),
    admin_notes TEXT
);

-- 2. Create indexes for fast querying
CREATE INDEX IF NOT EXISTS idx_requests_status ON public.integration_requests (status);
CREATE INDEX IF NOT EXISTS idx_requests_created_at ON public.integration_requests (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_requests_user_id ON public.integration_requests (user_id);

-- 3. Enable Realtime for live updates on the Admin Website
ALTER PUBLICATION supabase_realtime ADD TABLE public.integration_requests;

-- 4. Enable Row Level Security (RLS)
ALTER TABLE public.integration_requests ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (for the Telegram Bot backend)
CREATE POLICY "Allow service role full access" 
ON public.integration_requests 
FOR ALL 
TO service_role 
USING (true) 
WITH CHECK (true);

-- Allow authenticated admins to view and update
CREATE POLICY "Allow authenticated admins to read and update" 
ON public.integration_requests 
FOR ALL 
TO authenticated 
USING (true) 
WITH CHECK (true);
```

---

## 4. Bot Code Integration (Sending Data to Supabase)

### Step 4.1: Install Dependencies
In `requirements.txt`, add `supabase`:
```text
supabase>=2.3.0
```

### Step 4.2: Add Environment Variables
Add to `.env` (and your Render Environment Variables):
```env
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJh...your-service-role-key
```
*(Use the `service_role` secret key on the bot backend so it can securely insert records directly).*

### Step 4.3: Python Helper Function for Bot (`main.py`)

Add this to `main.py`:

```python
import os
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase_client: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized successfully.")
    except Exception as e:
        logger.error("Failed to initialize Supabase client: %s", e)


def save_integration_request_to_supabase(payload: dict) -> bool:
    """Inserts a verified integration request into Supabase."""
    if not supabase_client:
        logger.warning("Supabase client not configured. Skipping database record.")
        return False

    try:
        data = {
            "user_id": payload.get("user_id"),
            "user_name": payload.get("user_name"),
            "user_handle": payload.get("user_handle"),
            "college_name": payload.get("college_name"),
            "branch_name": payload.get("branch_name"),
            "subject_name": payload.get("subject_name"),
            "spreadsheet_title": payload.get("spreadsheet_title"),
            "spreadsheet_url": payload.get("spreadsheet_url"),
            "document_id": payload.get("document_id"),
            "tab_names": payload.get("tab_names", []),
            "student_count": payload.get("student_count", 0),
            "session_count": payload.get("session_count", 0),
            "status": "pending",
        }
        res = supabase_client.table("integration_requests").insert(data).execute()
        logger.info("Saved request to Supabase successfully: %s", res.data)
        return True
    except Exception as exc:
        logger.error("Error inserting request into Supabase: %s", exc)
        return False
```

And in `received_subject()` right alongside forwarding to `ADMIN_ID`:

```python
# Save to Supabase for the Admin Website
save_integration_request_to_supabase({
    "user_id": user.id,
    "user_name": user.full_name,
    "user_handle": user.username,
    "college_name": college,
    "branch_name": branch,
    "subject_name": subject,
    "spreadsheet_title": sheet_title,
    "spreadsheet_url": sheet_url,
    "document_id": doc_id,
    "tab_names": list(sheet_metrics.keys()),
    "student_count": total_students,
    "session_count": total_sessions,
})
```

---

## 5. Admin Website Integration (Frontend)

Your Admin Website can display all incoming requests in real-time.

### Step 5.1: Client-Side Supabase Setup (`lib/supabase.ts`)
```typescript
import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

export const supabase = createClient(supabaseUrl, supabaseAnonKey);
```

### Step 5.2: React / Next.js Admin Dashboard Component
```tsx
import React, { useEffect, useState } from 'react';
import { supabase } from '@/lib/supabase';

interface IntegrationRequest {
  id: string;
  created_at: string;
  user_id: number;
  user_name: string;
  user_handle: string | null;
  college_name: string;
  branch_name: string;
  subject_name: string;
  spreadsheet_title: string;
  spreadsheet_url: string;
  student_count: number;
  session_count: number;
  status: 'pending' | 'under_review' | 'integrated' | 'rejected';
}

export default function AdminRequestsPage() {
  const [requests, setRequests] = useState<IntegrationRequest[]>([]);
  const [loading, setLoading] = useState(true);

  // 1. Initial Data Fetch
  const fetchRequests = async () => {
    setLoading(true);
    const { data, error } = await supabase
      .from('integration_requests')
      .select('*')
      .order('created_at', { ascending: false });

    if (!error && data) {
      setRequests(data);
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchRequests();

    // 2. Real-time Subscription for Instant Updates
    const channel = supabase
      .channel('realtime_requests')
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'integration_requests' },
        (payload) => {
          if (payload.eventType === 'INSERT') {
            setRequests((prev) => [payload.new as IntegrationRequest, ...prev]);
          } else if (payload.eventType === 'UPDATE') {
            setRequests((prev) =>
              prev.map((item) => (item.id === payload.new.id ? (payload.new as IntegrationRequest) : item))
            );
          }
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  // 3. Update Request Status (e.g. Approve / Mark Integrated)
  const updateStatus = async (id: string, newStatus: IntegrationRequest['status']) => {
    const { error } = await supabase
      .from('integration_requests')
      .update({ status: newStatus, updated_at: new Date().toISOString() })
      .eq('id', id);

    if (error) {
      alert('Failed to update status: ' + error.message);
    }
  };

  return (
    <div style={{ padding: '2rem', fontFamily: 'system-ui, sans-serif' }}>
      <h1>📋 Subject Integration Requests</h1>
      <p>Live stream of incoming verified Google Sheets from Telegram.</p>

      {loading ? (
        <p>Loading requests...</p>
      ) : requests.length === 0 ? (
        <p>No integration requests yet.</p>
      ) : (
        <table border={1} cellPadding={10} style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: '#f4f4f5', textAlign: 'left' }}>
              <th>Time</th>
              <th>College</th>
              <th>Branch</th>
              <th>Subject</th>
              <th>Metrics</th>
              <th>Teacher / User</th>
              <th>Sheet</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {requests.map((req) => (
              <tr key={req.id}>
                <td>{new Date(req.created_at).toLocaleString()}</td>
                <td><strong>{req.college_name}</strong></td>
                <td>{req.branch_name}</td>
                <td><span style={{ color: '#2563eb' }}>{req.subject_name}</span></td>
                <td>
                  👥 {req.student_count} students<br />
                  📅 {req.session_count} sessions
                </td>
                <td>
                  {req.user_name} <br />
                  {req.user_handle ? `@${req.user_handle}` : `ID: ${req.user_id}`}
                </td>
                <td>
                  <a href={req.spreadsheet_url} target="_blank" rel="noreferrer">
                    View Sheet ↗
                  </a>
                </td>
                <td>
                  <span style={{
                    padding: '4px 8px',
                    borderRadius: '4px',
                    fontSize: '12px',
                    fontWeight: 'bold',
                    background:
                      req.status === 'integrated' ? '#dcfce7' :
                      req.status === 'rejected' ? '#fee2e2' :
                      req.status === 'under_review' ? '#fef3c7' : '#e0e7ff',
                    color:
                      req.status === 'integrated' ? '#15803d' :
                      req.status === 'rejected' ? '#b91c1c' :
                      req.status === 'under_review' ? '#b45309' : '#4338ca',
                  }}>
                    {req.status.toUpperCase()}
                  </span>
                </td>
                <td>
                  <select
                    value={req.status}
                    onChange={(e) => updateStatus(req.id, e.target.value as any)}
                  >
                    <option value="pending">Pending</option>
                    <option value="under_review">Under Review</option>
                    <option value="integrated">Integrated</option>
                    <option value="rejected">Rejected</option>
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

---

## 6. Closing the Loop: Notifying Users from Website

When an admin updates a request to **"Integrated"** on the website, your website backend can automatically notify the user back on Telegram:
```typescript
// Website API route: /api/notify-user
await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    chat_id: req.user_id,
    text: `🎉 Good news! Your subject "${req.subject_name}" for ${req.college_name} has been integrated successfully!`,
  }),
});
```

---

## 7. Summary Checklist

| Task | Where | Status |
|---|---|---|
| **Bot URL Detection & Syntax Validation** | Telegram Bot (`validator.py`) | ✅ Completed & Live |
| **Interactive 3-Step Questionnaire** | Telegram Bot (`main.py`) | ✅ Completed & Live |
| **Telegram Admin Chat Notification** | Telegram Bot (`ADMIN_ID`) | ✅ Completed & Live |
| **Create Supabase Table** | Supabase SQL Editor | 📝 Run SQL query in Section 3.1 |
| **Add Supabase Client in Bot** | Telegram Bot (`main.py`) | 📝 Add helper in Section 4.3 |
| **Admin Website Real-time Dashboard** | Web App (React/Next.js) | 📝 Add Component in Section 5.2 |
