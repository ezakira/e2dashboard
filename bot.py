import logging
import random
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters
)
from telegram.error import BadRequest

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import NoSuchElementException, TimeoutException
import supabase
import os
from dotenv import load_dotenv
from pathlib import Path
from flask import Flask, Response
from datetime import datetime, timezone, timedelta



app = Flask(__name__)

@app.route('/healthz')
def health_check():
    return Response("OK", status=200)

# Run in a separate thread
import threading
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

HELP_PATH = Path(__file__).parent / "help.html"
with open(HELP_PATH, "r", encoding="utf-8") as f:
    HELP_TEXT = f.read()

load_dotenv()
USER_BUSY = {}
# Removed ACTIVE_OPERATIONS and cancellation logic

# Configuration
LOGIN_URL = "https://e2.partners/page/affiliate/login.jsp"
DASHBOARD_URL = "https://e2.partners/page/affiliate/index.jsp"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CACHE_DURATION = 300  # 5 minutes cache

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client = supabase.create_client(SUPABASE_URL, SUPABASE_KEY)

# Conversation states
USERNAME, PASSWORD = range(2)
REMOVE_USERNAME = 2  # New state for removal

# Configure logger
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

MYT = timezone(timedelta(hours=8))

def get_malaysia_time():
    return datetime.now(MYT)

def create_driver():

    chrome_options = Options()
    
    # Enhanced stealth options
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    # Additional stealth parameters
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--allow-running-insecure-content")
    chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    service = Service(executable_path="/usr/local/bin/chromedriver")
    
    try:
        driver = webdriver.Chrome(service=service, options=chrome_options)

        # Mask headless browser as normal browser
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
        })
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            '''
        })
        return driver
    except Exception as e:
        logger.error(f"Driver creation failed: {str(e)}")
        return None
    
    

def validate_credentials(username: str, password: str) -> bool:
    """Validate affiliate credentials by attempting login and return available currencies"""
    driver = create_driver()
    if not driver:
        return False
    
    try:
        driver.get(LOGIN_URL)
        
        
        # Fill credentials
        username_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "userId"))
        )
        username_field.send_keys(username)
        
        password_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "password"))
        )
        password_field.send_keys(password)
        
        # Click login button
        login_button = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "login"))
        )
        login_button.click()
        
        # Check if login was successful
        WebDriverWait(driver, 15).until(
            EC.url_contains("index.jsp")
        )
        
        # Get available currencies
        currency_options = get_available_currencies(driver)
        logger.info(f"Found {len(currency_options)} currencies for {username}")
        
        return True
    except Exception as e:
        logger.error(f"Validation failed: {str(e)}")
        return False
    finally:
        driver.quit()

def get_available_currencies(driver):
    """Get available currencies from dropdown"""
    try:
        # Wait for currency dropdown to be present
        currency_dropdown = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "dashboardCurrency"))
        )
        
        select = Select(currency_dropdown)
        options = []
        
        for option in select.options:
            currency_value = option.get_attribute('value')
            currency_text = option.text.strip()
            options.append({
                'value': currency_value,
                'text': currency_text
            })
        
        return options
    except Exception as e:
        logger.error(f"Error getting currencies: {str(e)}")
        return []

def change_currency(driver, currency_value):
    """Change dashboard currency"""
    try:
        # Wait for currency dropdown to be present
        currency_dropdown = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "dashboardCurrency"))
        )
        
        select = Select(currency_dropdown)
        select.select_by_value(currency_value)
        
        # Wait for page to update - we'll wait for commission elements to update
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "thisPeriodCommission"))
        )
        
        # Additional wait to ensure data has refreshed
        time.sleep(1)
        return True
    except Exception as e:
        logger.error(f"Error changing currency to {currency_value}: {str(e)}")
        return False

def scrape_single_currency(driver):
    """Scrape data for the current currency"""
    # Helper function to extract amount and detect negative (red color)
    def extract_amount(cell):
        try:
            # Look for <span> elements with red color styling
            red_spans = cell.find_elements(
                By.XPATH, ".//span[contains(@style, 'color:red') or contains(@style, 'color: red')]"
            )
            if red_spans:
                amount_text = red_spans[0].text.strip()
                # Add minus sign directly to amount without space
                if amount_text and not amount_text.startswith('-'):
                    return '-' + amount_text.replace(" ", "")
                return amount_text.replace(" ", "")
            return cell.text.strip().replace(" ", "")
        except:
            return cell.text.strip().replace(" ", "")

    try:
        # Step 3: Scrape Active Players
        active_players = {}
        try:
            this_period = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "thisPeriodActivePlayer"))
            ).text
            last_period = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "lastPeriodActivePlayer"))
            ).text
            active_players = {
                "this_period": this_period,
                "last_period": last_period
            }
        except Exception as e:
            logger.warning(f"Could not scrape active players: {str(e)}")

        commissions = {}
        try:
            this_period_commission = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "thisPeriodCommission"))
            ).text
            last_period_commission = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "lastPeriodCommission"))
            ).text

            # Extract currency symbol from commission values
            currency = ''
            if this_period_commission and this_period_commission[0] in '$€£¥₩₹₽₿₺₴₸₲₵₡₪₫':
                currency = this_period_commission[0]

            commissions = {
                "this_period": this_period_commission,
                "last_period": last_period_commission,
                "currency": currency
            }
        except Exception as e:
            logger.warning(f"Could not scrape commissions: {str(e)}")

        # New: Scrape Withdrawable Amount
        withdrawable = ""
        try:
            # Wait for user info element
            user_info = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "user-info"))
            )

            # Extract money elements
            money_element = user_info.find_element(By.CLASS_NAME, "money")
            symbol = money_element.find_element(By.ID, "navBarMoney").text.strip()
            amount = money_element.find_element(By.ID, "navBarAvailable").text.strip()

            withdrawable = f"`{symbol}` `{amount}`"
        except Exception as e:
            logger.warning(f"Could not scrape withdrawable amount: {str(e)}")

        # Step 4: Scrape sections using multiple identification methods
        report_data = {}
        section_selectors = {
            "Registered Users": {
                "title": "Registered Users",
                "container": "div:nth-child(5)"  # Adjust based on screenshot
            },
            "First Deposit": {
                "title": "First Deposit",
                "container": "div:nth-child(3) > div:nth-child(3)"  # Adjust based on screenshot
            },
            "Deposit": {
                "title": "Deposit",
                "container": "div:nth-child(4) > div:nth-child(1)"  # Adjust based on screenshot
            },
            "Withdrawal": {
                "title": "Withdrawal",
                "container": "div:nth-child(4) > div:nth-child(2)"  # Adjust based on screenshot
            },
            "Affiliate Profit & Loss": {
                "title": "Affiliate Profit & Loss",
                "container": "div:nth-child(7)"  # Adjust based on screenshot
            },
            "Turnover": {
                "title": "Turnover",
                "container": "div:nth-child(6) > div:nth-child(2)"  # Adjust based on screenshot
            }
        }

        for section_name, selector in section_selectors.items():
            try:
                # Try multiple methods to find the section
                section = None

                # Method 1: By exact title text
                try:
                    section = driver.find_element(
                        By.XPATH,
                        f"//h2[normalize-space()='{selector['title']}']/ancestor::div[contains(@class, 'panel')]"
                    )
                except NoSuchElementException:
                    pass

                # Method 2: By container position (CSS selector)
                if not section:
                    try:
                        section = driver.find_element(
                            By.CSS_SELECTOR,
                            f"div.panel > {selector['container']}"
                        )
                    except NoSuchElementException:
                        pass

                # Method 3: Fallback to general panel search
                if not section:
                    panels = driver.find_elements(By.CLASS_NAME, "panel")
                    for panel in panels:
                        if selector['title'] in panel.text:
                            section = panel
                            break

                if not section:
                    logger.warning(f"Section not found: {section_name}")
                    continue

                # Extract table data
                table = section.find_element(By.TAG_NAME, "table")
                headers = [th.text.strip() for th in table.find_elements(By.TAG_NAME, "th")]

                rows = []
                period_mapping = {
                    "Today": "Today",
                    "Yesterday": "Yesterday",
                    "This Week": "This Week",
                    "This Month": "This Month",
                    "Last Month": "Last Month"
                }

                # Special handling for Turnover section
                if section_name == "Turnover":
                    period_mapping = {
                        "This Month": "This Month",
                        "Last Month": "Last Month"
                    }

                # Pull every row
                if section_name == "Registered Users":
                    for tr in table.find_elements(By.TAG_NAME, "tr")[1:]:
                        cells = tr.find_elements(By.TAG_NAME, "td")
                        if len(cells) < 2:
                            continue

                        period = cells[0].text.strip()
                        period = period_mapping.get(period, period)

                        if period in period_mapping.values():
                            count = cells[1].text.strip()
                            rows.append([period, count])
                else:
                    for tr in table.find_elements(By.TAG_NAME, "tr")[1:]:
                        cells = tr.find_elements(By.TAG_NAME, "td")
                        if len(cells) < 3:
                            continue

                        period = cells[0].text.strip()
                        period = period_mapping.get(period, period)
                        if period not in period_mapping.values():
                            continue

                        count = cells[1].text.strip()
                        
                        # ONLY apply negative handling to Profit & Loss section
                        if section_name == "Affiliate Profit & Loss":
                            amount_str = extract_amount(cells[2])
                        else:
                            amount_str = cells[2].text.strip()

                        # Process currency symbol
                        CURRENCY_SYMBOLS = '$€£¥₩₹₽₿₺₴₸₲₵₡₪₫'
                        currency_sym = ''
                        
                        # Handle negative amounts with currency symbol
                        if amount_str.startswith('-') and len(amount_str) > 1:
                            # Check for currency symbol at position 1 (after minus)
                            if amount_str[1] in CURRENCY_SYMBOLS:
                                currency_sym = amount_str[1]
                                amount_str = '-' + amount_str[2:].strip()
                            else:
                                # Keep minus sign and continue processing
                                amount_str = amount_str.strip()
                        # Handle positive amounts with currency symbol
                        elif amount_str and amount_str[0] in CURRENCY_SYMBOLS:
                            currency_sym = amount_str[0]
                            amount_str = amount_str[1:].strip()

                        rows.append([period, count, amount_str, currency_sym])

                if rows:
                    report_data[section_name] = {
                        "headers": headers,
                        "rows": rows,
                        "currency": commissions.get("currency", "")
                    }

            except Exception as e:
                logger.error(f"Error processing section {section_name}: {str(e)}")
                continue

        return {
            "active_players": active_players,
            "commissions": commissions,
            "sections": report_data,
            "withdrawable": withdrawable
        }
    except Exception as e:
        logger.error(f"Scraping failed: {str(e)}")
        return None

def scrape_data(username: str, password: str, user_id: int):
    """Scrape data for all available currencies"""
    # Create driver instance
    driver = create_driver()
    if not driver:
        return None
        
    try:
        # Step 1: Login
        driver.delete_all_cookies()
        driver.get(LOGIN_URL)
        time.sleep(1)
        
        # Fill credentials
        username_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "userId"))
        )
        username_field.send_keys(username)
        
        
        password_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "password"))
        )
        password_field.send_keys(password)
        
        
        # Click login button
        login_button = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "login"))
        )
        login_button.click()
        
        # Step 2: Wait for dashboard
        WebDriverWait(driver, 20).until(
            EC.url_contains("index.jsp")
        )
        
        # Wait for critical elements to load
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, "panel"))
        )
        
        # Get available currencies
        currencies = get_available_currencies(driver)
        if not currencies:
            logger.info("No currencies found, scraping default")
            return {'DEFAULT': scrape_single_currency(driver)}
        
        # Scrape data for each currency
        currency_reports = {}
        for currency in currencies:
            logger.info(f"Scraping for currency: {currency['text']}")
            
            # Change currency
            if change_currency(driver, currency['value']):
                # Scrape data for this currency
                report = scrape_single_currency(driver)
                if report:
                    currency_reports[currency['text']] = report
            else:
                logger.error(f"Failed to change to currency: {currency['text']}")
        
        return currency_reports
        
    except Exception as e:
        logger.error(f"Scraping failed: {str(e)}")
        return None
    finally:
        if driver:
            driver.quit()

def format_report(data, account_name: str = "", currency: str = "", last_update: datetime = None):
    """Format report in Markdown for Telegram (parse_mode='Markdown')."""
    if not data:
        return "_No data available. Please try again later._"

    current_myt = get_malaysia_time()
    
    # Handle the last_update parameter correctly
    if last_update is None:
        # Use current MYT time if no last_update provided
        report_time = current_myt
    else:
        # Convert any datetime to MYT
        if last_update.tzinfo is None:
            # If naive datetime, assume UTC
            report_time = last_update.replace(tzinfo=timezone.utc).astimezone(MYT)
        else:
            # Convert to MYT if it has timezone info
            report_time = last_update.astimezone(MYT)

    # Format the header date
    current_date = report_time.strftime("%a, %B %d")
    total_width = 65  # tweak to align the date
    
    # Add currency to account name if provided
    display_name = f"⟪ {account_name} ⟫ ({currency})" if currency else account_name
    padding = max(total_width - len(display_name) - len(current_date), 1)
    header = f"*{display_name}{' ' * padding}{current_date}*\n"

    msg = [header]

    if withdrawable := data.get("withdrawable"):
        msg.append(f"*Withdrawable:* {withdrawable}")

    # Initialize variables to avoid reference before assignment
    ap = data.get("active_players", {})
    comm = data.get("commissions", {})
    
    # Active Players and Commissions side by side
    if ap or comm:
        # Section headers
        active_header = "⦗ Active Players ⦘"
        comm_header = "⦗ Commissions ⦘"
        padding = 30 - len(active_header)  # Adjust spacing between columns
        section_headers = f"*{active_header}{' ' * padding}{comm_header}*"
        msg.append(section_headers)
        
        msg.append("*━━━━━━━━━━━━━━━━━━━━*")

        # This Period row
        ap_this = ap.get("this_period", "N/A")
        comm_this = comm.get("this_period", "N/A")
        comm_this_clean = comm_this.strip()
        if comm_this_clean and not comm_this_clean[0].isdigit():
            # Find where numbers start in the string
            for i, char in enumerate(comm_this_clean):
                if char.isdigit() or char in '.,-':
                    currency_part = comm_this_clean[:i].strip()
                    amount_part = comm_this_clean[i:].strip()
                    comm_this = f"{currency_part} {amount_part}"
                    break
        
        left_part = f"This Period *≅* `{ap_this}`"
        padding = 30 - len(left_part)
        right_part = f"This Period - `{comm_this}`"
        msg.append(f"{left_part}{' ' * padding}{right_part}")

        # Last Period row
        ap_last = ap.get("last_period", "N/A")
        comm_last = comm.get("last_period", "N/A")
        
        # Apply the same cleaning to last period
        comm_last_clean = comm_last.strip()
        if comm_last_clean and not comm_last_clean[0].isdigit():
            for i, char in enumerate(comm_last_clean):
                if char.isdigit() or char in '.,-':
                    currency_part = comm_last_clean[:i].strip()
                    amount_part = comm_last_clean[i:].strip()
                    comm_last = f"{currency_part} {amount_part}"
                    break
        
        left_part = f"Last Period *≅* `{ap_last}`"
        padding = 30 - len(left_part)
        right_part = f"Last Period - `{comm_last}`"
        msg.append(f"{left_part}{' ' * padding}{right_part}")

        msg.append("*━━━━━━━━━━━━━━━━━━━━*")

    if "Registered Users" in data.get("sections", {}):
        ru_section = data["sections"]["Registered Users"]
        rows = ru_section.get("rows", [])
        
        msg.append("*⦗ Registered Users ⦘*")
        msg.append("*━━━━━━━━━━━━━━━━━━━━*")
        
        for row in rows:
            if len(row) >= 2:
                period, count = row[0], row[1]
                line = f"• {period} ⁃ `{count}`"
                msg.append(line)
        
        msg.append("*━━━━━━━━━━━━━━━━━━━━*")

    # Other sections
    sections = [
        ("First Deposit", "|", True),
        ("Deposit", "|", True),
        ("Withdrawal", "|", True),
        ("Affiliate Profit & Loss", "=", False),
        ("Turnover", "=", False),
    ]

    for name, sep, parens in sections:
        if name not in data.get("sections", {}):
            continue

        section_data = data["sections"][name]
        rows = section_data.get("rows", [])
        currency_sym = section_data.get("currency", "")
        
        msg.append(f"*⦗ {name} ⦘*")
        msg.append("*━━━━━━━━━━━━━━━━━━━━*")

        for row in rows:
            if len(row) >= 4:  # period, count, amount, currency
                period, count, amount, row_currency = row[0], row[1], row[2], row[3]
                effective_currency = row_currency if row_currency else currency_sym
            else:
                period, count, amount = row[0], row[1], row[2]
                effective_currency = currency_sym

            count_md = f"`{count}`"

            # Special handling for Profit & Loss negative amounts
            if name == "Affiliate Profit & Loss":
                # Format negative amounts without space after minus sign
                if amount.startswith('-') and len(amount) > 1:
                    # Remove any existing spaces and format as '-123.45'
                    cleaned_amount = amount.replace(" ", "")
                    amt_md = f"`{cleaned_amount}`"
                else:
                    amt_md = f"`{amount}`"
            else:
                # For other sections, format with currency symbol
                if effective_currency:
                    amt_md = f"`{effective_currency} {amount}`"
                else:
                    amt_md = f"`{amount}`"

            if parens:
                line = f"• {period} {sep} {count_md} {sep} ( {amt_md} )"
            else:
                line = f"• {period} {sep} {amt_md}"

            msg.append(line)

        msg.append("*━━━━━━━━━━━━━━━━━━━━*")

    # Footer (italic)

        # Footer (italic)
    timestamp = report_time.strftime("%Y-%m-%d %H:%M:%S (MYT)")
    msg.append(f"_Last updated: {timestamp}_")

    return "\n".join(msg)


# Database functions
def addaffiliate_account(user_id: int, username: str, password: str):
    """Add affiliate account to database"""
    try:
        response = supabase_client.table('affiliate_accounts').insert({
            'user_id': user_id,
            'username': username,
            'password': password
        }).execute()
        
        if response.data:
            return True
        return False
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return False
# Add this in the "Database functions" section
def remove_account_from_db(user_id: int, username: str) -> bool:
    """Remove affiliate account from database"""
    try:
        response = supabase_client.table('affiliate_accounts').delete().eq('user_id', user_id).eq('username', username).execute()
        # Check if any rows were deleted
        if response.data and len(response.data) > 0:
            return True
        return False
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return False

def get_user_accounts(user_id: int):
    """Get all affiliate accounts for a user"""
    try:
        response = supabase_client.table('affiliate_accounts').select(
            "username"
        ).eq('user_id', user_id).execute()
        
        return [account['username'] for account in response.data] if response.data else []
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return []

def get_account_credentials(user_id: int, username: str):
    """Get credentials for a specific account"""
    try:
        response = supabase_client.table('affiliate_accounts').select(
            "username", "password"
        ).eq('user_id', user_id).eq('username', username).execute()
        
        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return None

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with available commands"""
    commands = (
        "*E2 Dashboard bot!*\n\n"
        "*Available commands:*\n\n"
        "*• /addaff - add an affiliate account*\n"
        "*• /remove - remove a connected account*\n"
        "*• /fetch - fetch reports for your accounts*\n"
        "*• /accounts - list your saved accounts*\n"
        "*• /help - usage documentation*\n"
        "*• /report - report bugs & errors*\n"
    )
    await update.message.reply_text(commands, parse_mode="Markdown")



async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help instructions"""
    # (no more inline string here)
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user reports"""
    await update.message.reply_text(
        "*Drop me a message! Thank you!! @takt_akira *\n",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user reports"""
    await update.message.reply_text(
        "*Not yet functional.*",        parse_mode="Markdown"
    )
    # Add your reporting mechanism here (e.g., forward to admin channel)

async def addaff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the add affiliate account conversation"""
    user_id = update.message.from_user.id
    await update.message.reply_text(
        "*<Add an affiliate>*\n"
        "*Please enter the affiliate username:*",
        parse_mode="Markdown"
    )
    return USERNAME

async def handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store username and ask for password"""
    context.user_data['username'] = update.message.text
    await update.message.reply_text(
        "*Please enter the affiliate password:*",
        parse_mode="Markdown"
    )
    return PASSWORD

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate credentials and save account"""
    user_id = update.message.from_user.id
    context.user_data['user_id'] = user_id  # Store for cancellation
    
    # Cooldown check
    if USER_BUSY.get(user_id, False):
        await update.message.reply_text(
            "*Processing your previous request...*\n"
            "Use /cancel to abort current request",
            parse_mode="Markdown"
        )
        return
    
    # Set busy state
    USER_BUSY[user_id] = True
    
    try:
        password = update.message.text
        username = context.user_data['username']
        
        # Validate credentials
        await update.message.reply_text("*Validating credentials... (ETA≈ 7-12s)*", parse_mode="Markdown")
        if validate_credentials(username, password):
            # Save to database
            if addaffiliate_account(user_id, username, password):
                await update.message.reply_text(
                    f"*Account Added Successfully!*\n"
                    f"*E2 Affiliate `{username}` has been added to your account.*",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    "*Database Error*\n"
                    "*Failed to save account. Please try again later.*",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text(
                "*Invalid Credentials (401 Unauthorized)*\n"
                "*The username or password is incorrect. Please start over with /addaff*",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error in handle_password: {str(e)}")
    finally:
        # Clear busy state
        USER_BUSY[user_id] = False
        context.user_data.clear()
        return ConversationHandler.END
# Add these new handlers in the "Command handlers" section
async def remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start account removal process"""
    args = context.args
    user_id = update.message.from_user.id
    
    if args:
        # Direct removal via /remove username
        username = args[0]
        if remove_account_from_db(user_id, username):
            await update.message.reply_text(f"Removed `{username}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"`{username}` is not connected.", parse_mode="Markdown")
    else:
        # Start interactive removal
        await update.message.reply_text(
            "*Enter an affiliate username:*",
            parse_mode="Markdown"
        )
        return REMOVE_USERNAME

async def handle_remove_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle username input for removal"""
    user_id = update.message.from_user.id
    username = update.message.text
    
    if remove_account_from_db(user_id, username):
        await update.message.reply_text(f"Removed `{username}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"`{username}` is not connected.", parse_mode="Markdown")
    
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle various errors"""
    error = context.error
    
    if isinstance(error, BadRequest):
        if "Query is too old" in str(error):
            logger.warning("Ignoring old query error")
            return
        elif "terminated by other getUpdates request" in str(error):
            logger.error("Multiple bot instances detected")
            # Consider implementing a restart mechanism here
            return
    
    logger.error(f"Unhandled error: {error}", exc_info=True)

# Then in main() after building application:
   

async def fetch_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch reports with account selection menu"""
    user_id = update.message.from_user.id
    
    if USER_BUSY.get(user_id, False):
        await update.message.reply_text(
            "*Processing your previous request...*",
            parse_mode="Markdown"
        )
        return
    
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.message.reply_text(
            "No affiliate accounts yet. Use /addaff to add one."
        )
        return
    
    # Create inline keyboard with accounts
    keyboard = []
    for account in accounts:
        keyboard.append([InlineKeyboardButton(account, callback_data=f"fetch_{account}")])
    
    # Add "Fetch All" option if more than one account
    if len(accounts) > 1:
        keyboard.append([InlineKeyboardButton("fetch all (unstable)", callback_data="fetch_all")])  # Removed "unstable" label
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "*Accounts available:*",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def fetch_account_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle account selection for fetching reports with proper data storage"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # Set busy state
    USER_BUSY[user_id] = True
    
    try:
        data = query.data
        
        if data == "fetch_all":
            accounts = get_user_accounts(user_id)
            message = await query.message.reply_text("*Gathering reports for all accounts...(ETA≈ 8-15s)*", parse_mode="Markdown")
            
            # Initialize reports storage
            context.user_data.setdefault('reports', {})
            
            for account in accounts:
                creds = get_account_credentials(user_id, account)
                if not creds:
                    continue
                    
                report_data = scrape_data(
                    creds['username'], 
                    creds['password'], 
                    user_id
                )
                
                if report_data:
                    # Store report for navigation
                    context.user_data['reports'][account] = report_data
                    
                    if isinstance(report_data, dict) and len(report_data) > 0:
                        first_currency = next(iter(report_data.keys()))
                        report = format_report(report_data[first_currency], account, first_currency, last_update=query.message.date)
                        
                        if len(report_data) > 1:
                            keyboard = [
                                [
                                    InlineKeyboardButton("❮❮❮", callback_data=f"nav:{account}:{first_currency}:prev"),
                                    InlineKeyboardButton(first_currency, callback_data="none"),
                                    InlineKeyboardButton("❯❯❯", callback_data=f"nav:{account}:{first_currency}:next")
                                ]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await context.bot.send_message(
                                chat_id=query.message.chat_id,
                                text=report,
                                parse_mode="Markdown",
                                reply_markup=reply_markup
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=query.message.chat_id,
                                text=report,
                                parse_mode="Markdown"
                            )
                    else:
                        report = format_report(report_data, account, last_update=query.message.date)
                        await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=report,
                            parse_mode="Markdown"
                        )
                else:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"*Failed to fetch data for account `{account}`*",
                        parse_mode="Markdown"
                    )
            
            await message.delete()
            
        elif data.startswith("fetch_"):
            account = data.replace("fetch_", "")
            creds = get_account_credentials(user_id, account)
            
            if not creds:
                await query.edit_message_text(f"*Account `{account}` not found*", parse_mode="Markdown")
                return
            
            await query.edit_message_text(f"*Fetching {account}...(ETA≈ 6-10s)*", parse_mode="Markdown")
            report_data = scrape_data(
                creds['username'], 
                creds['password'], 
                user_id
            )
            
            if report_data:
                # Store report for navigation
                context.user_data.setdefault('reports', {})
                context.user_data['reports'][account] = report_data
                
                currencies = list(report_data.keys())
                current_currency = currencies[0]
                report = format_report(report_data[current_currency], account, current_currency, last_update=query.message.date)
                
                if len(currencies) > 1:
                    keyboard = [
                        [
                            InlineKeyboardButton("❮❮❮", callback_data=f"nav:{account}:{current_currency}:prev"),
                            InlineKeyboardButton(current_currency, callback_data="none"),
                            InlineKeyboardButton("❯❯❯", callback_data=f"nav:{account}:{current_currency}:next")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        text=report,
                        parse_mode="Markdown",
                        reply_markup=reply_markup
                    )
                else:
                    await query.edit_message_text(
                        text=report,
                        parse_mode="Markdown"
                    )
            else:
                await query.edit_message_text(f"*Failed to fetch {account}*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in fetch_account_report: {str(e)}")
    finally:
        # Clear busy state
        USER_BUSY[user_id] = False

async def handle_currency_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle currency navigation with persistent data"""
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(':')
    account = parts[1]
    current_currency = parts[2]
    direction = parts[3]
    
    # Retrieve stored reports
    reports = context.user_data.get('reports', {})
    
    if not reports:
        await query.edit_message_text(
            "*No reports available. Please fetch reports again.*",
            parse_mode="Markdown"
        )
        return
    
    report_data = reports.get(account)
    if not report_data:
        await query.edit_message_text(
            f"*Report data for {account} not found. Please refetch.*",
            parse_mode="Markdown"
        )
        return
    
    currencies = list(report_data.keys())
    
    if not currencies:
        await query.edit_message_text(
            "*No currency data available. Please fetch the report again.*",
            parse_mode="Markdown"
        )
        return
    
    current_index = currencies.index(current_currency)
    
    # Determine new currency
    if direction == 'next':
        new_index = (current_index + 1) % len(currencies)
    else:  # prev
        new_index = (current_index - 1) % len(currencies)
    
    new_currency = currencies[new_index]
    
    # Format report for new currency
    report = format_report(report_data[new_currency], account, new_currency, last_update=query.message.date)
    
    # Update navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("❮❮❮", callback_data=f"nav:{account}:{new_currency}:prev"),
            InlineKeyboardButton(new_currency, callback_data="none"),
            InlineKeyboardButton("❯❯❯", callback_data=f"nav:{account}:{new_currency}:next")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Edit existing message with new content
    try:
        await query.edit_message_text(
            text=report,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            # Ignore this specific error
            pass
        else:
            logger.error(f"Error editing message: {str(e)}")

async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all accounts for the user"""
    user_id = update.message.from_user.id
    accounts = get_user_accounts(user_id)
    
    if not accounts:
        await update.message.reply_text(
            "You haven't added any affiliate accounts yet. Use /addaff to add one."
        )
        return
    
    accounts_list = "\n".join([f"• `{acc}`" for acc in accounts])
    await update.message.reply_text(
        f"*Your Affiliate Accounts*\n\n{accounts_list}",
        parse_mode="Markdown"
    )

def main():
    """Start the bot"""
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add conversation handler for adding and removing accounts
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("addaff", addaff),
            CommandHandler("remove", remove_account)
        ],
        states={
            USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
            REMOVE_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_remove_username)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    # Add handlers with proper indentation
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("fetch", fetch_reports))
    application.add_handler(CommandHandler("accounts", list_accounts))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # Add handler for account selection
    application.add_handler(CallbackQueryHandler(fetch_account_report, pattern="^fetch_"))
    
    # Add handler for currency navigation
    application.add_handler(CallbackQueryHandler(handle_currency_navigation, pattern="^nav:"))
    application.add_error_handler(error_handler)
    application.run_polling()

if __name__ == "__main__":
    main()