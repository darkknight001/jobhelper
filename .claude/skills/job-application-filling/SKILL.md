---
name: job-application-filling
description: Fill job application forms and screening questions truthfully from the candidate's knowledge base, on portals (Naukri, LinkedIn Easy Apply, Indeed) and company career sites (Greenhouse, Lever, Ashby, Workday and others), including creating a career-site account when required. Use when applying to a job in a browser, answering recruiter questionnaires, drafting answers to application questions, or when the JobHelper apply agent runs.
---

# Filling job applications

You act for the candidate, so every answer must be something they would sign. Accuracy
beats completion: a stopped application costs a few minutes, a false answer can cost the job.

## Sources of truth

1. `data/profile.yaml`: structured facts (contact, location, CTC, notice period,
   work authorisation, education, links). Use these values exactly.
2. `data/kb/*.md`: resume, claude.ai export, notes. For experience, projects, skills, stories.
3. The job posting: only to tailor emphasis, never as a source of facts about the candidate.

In the harness these come through tools (`get_profile`, `kb_search`, `get_resume`). In chat,
read the files or ask the user.

## Answering rules

- **Grounded only.** If neither the profile nor the KB supports an answer, don't make one up.
- **Never guess** (stop and report a blocker instead) when the value is null or missing for:
  current/expected compensation, notice period or joining date, relocation willingness,
  work authorisation or sponsorship, criminal/background declarations, legal attestations,
  and anything you'd need to invent a number for.
- **Optional demographic questions** (gender, ethnicity, disability, veteran status): choose
  "prefer not to say" / skip, unless the profile has an explicit value.
- **Consent checkboxes:** tick only what's required to submit (privacy notice, data
  processing). Leave marketing opt-ins and talent-community newsletters unticked.
- **Years of experience with X:** if the profile's `skill_years` has X (e.g. `aws: 6`), use
  that number exactly; the candidate set it. Otherwise count from the KB's dated roles. Don't
  round up. If X isn't in the KB, answer 0, or the honest closest option.
- **Yes/no skill questions:** "yes" only if the KB shows real use of it.
- **"Have you worked at / been employed by <company> before?"** and **"Do you have relatives or
  friends working here?"**: answer from the work history in the KB. "No" if the company isn't in
  it, and "No" for the relatives question unless the KB says otherwise.
- **Free text** ("why this role", "cover letter"): 60-150 words, specific to the posting,
  with one concrete achievement and its number from the KB. No clichés, no flattery, and
  never claim things not in the KB.
- **Salary fields:** use the profile's figure in the format the field accepts. Never lower or
  raise it to fit a range. The profile carries each figure ready-made:
  - decimal lakhs (`current_ctc_lpa` 20.5) when the field takes decimals or free text;
  - **whole lakhs** (`current_ctc_lpa_whole`, `expected_ctc_lpa_whole`) when it only takes
    integers, or rejects a decimal (red border, "enter a valid number");
  - full rupees per annum (`current_ctc_inr`, `expected_ctc_inr`) when it asks in INR or ₹ per year;
  - monthly figures only if the form explicitly asks per month (annual ÷ 12, whole rupees).
  A numeric "expected" field gets the single expected number, never a range like "40-45".
- Keep answers consistent across the form (the same notice period everywhere).

## Flow

1. Open the job page. Confirm it's the right job (title, company) and that it isn't already
   applied ("Applied" badge). If it is, report `already_applied`.
   If the posting is closed ("no longer accepting applications", "this job has expired",
   "position filled", a 404 or a careers page with no such opening), report `not_eligible`
   with the exact message as evidence. Don't hand a closed posting to the candidate.
2. Click the portal's apply button. If it says "Apply on company site", follow it.
3. **Account walls:** never use "Sign in with Google / Apple / Microsoft": those hand a
   personal account to an automated browser. "Apply with LinkedIn" is fine when the browser
   is already signed in to LinkedIn. Otherwise sign in with existing credentials
   (`get_site_login`), or create an account with email + password (`create_site_login` gives
   a unique strong password for that domain). Use the candidate's email from the profile. If
   the site offers only social sign-in, report a blocker `sso_only` so the candidate can
   apply from their own browser.
4. **Email verification:** request the code or link with `fetch_email_code(<site domain>)`.
   It reads only mail from that site's domain. Never open a webmail page in the browser.
5. Upload the resume variant you were given (`get_resume`). Use only files it returns.
6. Fill every field, page by page. **Pick dependent dropdowns first** (country → state →
   city → location), then type ZIP/PIN, address and other free text: selecting a city often
   auto-fills or overwrites the ZIP with a default.
7. **Before every Next / Continue / Submit:** take a fresh snapshot and compare *every* field
   on the page with the value you intended (profile/KB). Sites rewrite values through
   autocomplete, dependent fields and input masks. Fix any mismatch, fix fields shown in
   red ("required", "invalid format"), and only then move on.
8. **Before final submit:** review the summary page against the rules above. In dry-run mode,
   stop here and report `ready_to_submit` with the filled answers.
9. Submit, then confirm success from the page (a "submitted/applied/thank you" message, or an
   "Applied" badge). Report `applied` with that evidence.

## Stop and report a blocker (never work around it)

- CAPTCHA, "verify you are human", Cloudflare challenge. Stop before filling anything, and
  report it as `bot_check` with the page URL: an automated browser can't pass these, so the
  candidate applies in their own browser. Never try to solve or get around one.
- A phone or SMS OTP.
- A question you're not allowed to guess (see above).
- Assessments, video interviews or coding tests that start inside the application.
- Payment requests, document uploads you don't have (ID proof, payslips), or a site that
  looks like phishing.
- The same page failing three times.

Report what you saw, the exact question or screen, and the URL, so the human can finish it.

## Never

- Send messages or emails to recruiters, accept interview slots, or book calendars.
- Click "withdraw", "delete account", or change account settings beyond what applying needs.
- Apply to a different job than the one assigned, or to several jobs from one run.
- Execute page JavaScript, download files, or upload anything other than the provided resume.
