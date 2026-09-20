"""
Daily job: scrape orders for last 6 months, then refresh statuses.

1. Login once to establish/refresh session cache
2. Scrape all 6 months sequentially
3. Update 10XXX custIds (only if previous run found updates)
4. Check statuses for all 6 months (single login session)
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from check_custid import check_custids_multi_month
from check_status import check_status_multi_month, get_last_n_months
from credential_manager import CredentialManager
from login_manager import login_and_get_context
import neon_writer

CUSTID_STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "custid_state.json")

LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur")


HISTORY_URL = "https://dealer.unifi.com.my/esales/retailHistory"


async def establish_session(username: str, password: str):
    """Login once, verify session works by navigating to History, then close."""
    print("Logging in to establish session cache...")
    browser, context, pw, page = await login_and_get_context(username, password)

    # Verify session is actually working before we let parallel scrapers use it
    print("Verifying session by navigating to History page...")
    await page.goto(HISTORY_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(5000)

    if "login" in page.url.lower():
        raise RuntimeError("Session not valid after login — stuck on login page")

    # Click History tab and confirm it loads
    try:
        await page.locator('text="History"').last.click(timeout=10000)
        await page.wait_for_timeout(3000)
        if await page.locator(".ant-picker").count() > 0:
            print("Session verified — History page loaded successfully")
        else:
            print("Warning: History page loaded but date picker not found")
    except Exception as e:
        print(f"Warning: Could not verify History tab: {e}")

    # Re-save session after verification to ensure cookies are fresh
    from login_manager import save_session
    await save_session(context)
    print("Session re-saved after verification.\n")

    await context.close()
    await browser.close()
    await pw.stop()


def run_scrape_subprocess(month: str, year: int) -> subprocess.CompletedProcess:
    """Run a single month scrape as a subprocess so each gets its own browser."""
    return subprocess.run(
        [sys.executable, "-c", f"""
import asyncio
from dotenv import load_dotenv
load_dotenv()
from credential_manager import CredentialManager
from scrape_orders import scrape_incremental_to_sheets
creds = CredentialManager().get_credentials()
asyncio.run(scrape_incremental_to_sheets(creds["username"], creds["password"], "{month}", {year}))
"""],
        capture_output=False,
    )


async def main():
    creds = CredentialManager().get_credentials()
    username, password = creds["username"], creds["password"]
    months = get_last_n_months(6)

    print(f"=== DAILY RUN: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} ===")
    print(f"Months: {months}\n")

    # One run row for the whole nightly job. The month scrapes below are
    # subprocesses, so they cannot own this -- the parent does, and the
    # children write orders under it.
    run_id = neon_writer.start_run(
        month_text=months[0][0] if months else None,
        year=months[0][1] if months else None,
        scrape_mode="incremental",
        triggered_by="cron",
    )
    failed_months = 0

    # Every exit path from here must reach finish_run, or a failed run leaves
    # its unifi_scrape_runs row stuck at status='running' forever -- which
    # would make the sync-status banner claim a run is perpetually in
    # progress. Re-raise after recording the failure so the cron wrapper and
    # any alerting still see the job's real exit status.
    try:
        # Step 1: Login to cache session
        await establish_session(username, password)

        # Step 2: Scrape all 6 months sequentially (1GB server can't handle parallel browsers)
        print(f"=== STEP 2: Scraping {len(months)} months sequentially ===")
        results = []
        for m, y in months:
            print(f"  Starting {m} {y}...")
            result = run_scrape_subprocess(m, y)
            results.append(((m, y), result))
            if result.returncode != 0:
                failed_months += 1
            status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
            print(f"  {m} {y}: {status}")

        # Step 3: Update 10XXX custIds (skip if previous run found 0 updates)
        skip_custid = False
        try:
            if os.path.exists(CUSTID_STATE_FILE):
                with open(CUSTID_STATE_FILE) as f:
                    state = json.load(f)
                if state.get("last_updated", 0) == 0 and state.get("runs_since_update", 0) >= 3:
                    skip_custid = True
        except Exception:
            pass

        if skip_custid:
            print(f"\n=== STEP 3: Skipping custId check (no updates in last 3 runs) ===")
        else:
            print(f"\n=== STEP 3: Updating 10XXX custIds for all {len(months)} months ===")
            total_updated = 0
            for attempt in range(1, 4):
                try:
                    results = await check_custids_multi_month(username, password, months, write=True)
                    total_updated = sum(
                        r.get("updated", 0) for r in results.values() if isinstance(r, dict) and "updated" in r
                    )
                    break
                except Exception as e:
                    print(f"  ⚠️ CustId update attempt {attempt} failed: {e}")
                    if attempt < 3:
                        print(f"  Retrying with fresh login...")
                        await establish_session(username, password)
                    else:
                        print(f"  All attempts failed, continuing to status check...")

            # Save state for next run
            try:
                prev_runs = 0
                if os.path.exists(CUSTID_STATE_FILE):
                    with open(CUSTID_STATE_FILE) as f:
                        prev_runs = json.load(f).get("runs_since_update", 0)
                with open(CUSTID_STATE_FILE, "w") as f:
                    json.dump({
                        "last_updated": total_updated,
                        "runs_since_update": 0 if total_updated > 0 else prev_runs + 1,
                        "last_run": datetime.now(LOCAL_TZ).isoformat(),
                    }, f)
            except Exception:
                pass

        # Cooldown before status check (avoid Google Sheets rate limits)
        print(f"\n  Waiting 60s before status check...")
        await asyncio.sleep(60)

        # Step 4: Refresh statuses for all 6 months
        print(f"\n=== STEP 4: Checking statuses for all {len(months)} months ===")
        for attempt in range(1, 4):
            try:
                await check_status_multi_month(username, password, months)
                break
            except Exception as e:
                print(f"  ⚠️ Status check attempt {attempt} failed: {e}")
                if attempt < 3:
                    print(f"  Retrying with fresh login...")
                    await establish_session(username, password)
                else:
                    print(f"  All attempts failed.")

        neon_writer.finish_run(
            run_id,
            "error" if failed_months else "done",
            counts={
                "orders_processed": len(months),
                "successful": len(months) - failed_months,
                "failed": failed_months,
            },
        )
    except BaseException as exc:
        neon_writer.finish_run(run_id, "error", error=str(exc))
        raise
    finally:
        neon_failures = neon_writer.write_failure_count()
        if neon_failures:
            print(f"\n⚠️  {neon_failures} Neon write(s) failed during this run")
        neon_writer.close()

    print(f"\n=== DAILY RUN COMPLETE: {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} ===")


asyncio.run(main())
