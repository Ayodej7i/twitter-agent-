"""
Manual Login Helper - Guides user through login process
"""

import sys
from pathlib import Path
import time

# Add project root to path
sys.path.append(str(Path(__file__).parent))

from core.improved_monitor import improved_monitor

def manual_login_helper():
    """Help user manually login and save session"""
    
    print("🔧 Manual Login Helper")
    print("=" * 50)
    print("This will help you login to Twitter/X manually and save the session")
    print("so the agent can stay logged in automatically.")
    print("=" * 50)
    
    try:
        print("\n1. Setting up browser...")
        if not improved_monitor.setup_browser():
            print("❌ Browser setup failed")
            return False
        
        print("✅ Browser setup successful")
        
        print("\n2. Opening Twitter login page...")
        improved_monitor.page.goto("https://x.com/i/flow/login", wait_until="networkidle")
        time.sleep(3)
        
        print("✅ Login page opened")
        
        print("\n3. MANUAL LOGIN REQUIRED:")
        print("   • Please login manually in the browser window")
        print("   • Enter your username: (from .env TWITTER_USERNAME)")
        print("   • Enter your password: (from .env TWITTER_PASSWORD)")
        print("   • Complete any 2FA if required")
        print("\n   ⏳ Waiting for you to complete login...")
        
        # Wait for user to complete login manually
        max_wait_time = 300  # 5 minutes
        wait_interval = 5
        elapsed = 0
        
        while elapsed < max_wait_time:
            # Check if login successful
            if improved_monitor.check_existing_session():
                print("✅ Login detected!")
                improved_monitor.logged_in = True
                break
            
            print(f"   Checking... ({elapsed//60}:{elapsed%60:02d} elapsed)")
            time.sleep(wait_interval)
            elapsed += wait_interval
        
        if improved_monitor.logged_in:
            print("\n4. Saving session...")
            improved_monitor.save_session()
            print("✅ Session saved!")
            
            # Test session persistence
            print("\n5. Testing session persistence...")
            improved_monitor.close()
            
            time.sleep(2)
            
            print("6. Reopening browser...")
            if improved_monitor.setup_browser():
                if improved_monitor.check_existing_session():
                    print("✅ Session persistence working!")
                    print("\n🎉 SUCCESS! Your agent can now stay logged in!")
                    print("\nYou can now run: python main.py")
                    improved_monitor.close()
                    return True
                else:
                    print("⚠️  Session persistence test failed")
            else:
                print("❌ Could not reopen browser")
        else:
            print("❌ Login not completed within 5 minutes")
        
        improved_monitor.close()
        return False
        
    except Exception as e:
        print(f"❌ Error: {e}")
        try:
            improved_monitor.close()
        except:
            pass
        return False

if __name__ == "__main__":
    print("📋 Instructions:")
    print("1. Run this script")
    print("2. A browser window will open")
    print("3. Login to Twitter/X manually in that window")
    print("4. The script will detect your login and save the session")
    print("5. After that, the agent can stay logged in automatically")
    print("\nReady to start? (Press Enter)")
    input()
    
    success = manual_login_helper()
    
    if success:
        print("\n✅ All set! Your Twitter agent can now:")
        print("   • Stay logged in automatically")
        print("   • Monitor accounts listed in config/persona.py")
        print("   • Generate fan responses using your persona config")
        print("   • Post controversial but safe tweets")
    else:
        print("\n⚠️  Issues encountered. Try:")
        print("   • Check your internet connection")
        print("   • Make sure you can login to Twitter/X normally")
        print("   • Try running the script again")
