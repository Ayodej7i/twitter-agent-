"""
Setup script for Twitter Controversial Agent
"""

import subprocess
import sys
import os
from pathlib import Path

def run_command(command, description):
    """Run a command and handle errors"""
    print(f"\n{'='*50}")
    print(f"Running: {description}")
    print(f"Command: {command}")
    print('='*50)
    
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ {description} completed successfully")
            if result.stdout:
                print(f"Output: {result.stdout}")
        else:
            print(f"✗ {description} failed")
            print(f"Error: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ {description} failed with exception: {e}")
        return False
    
    return True

def main():
    """Main setup function"""
    print("Twitter Controversial Agent Setup")
    print("="*50)
    
    # Check Python version
    if sys.version_info < (3, 8):
        print("✗ Python 3.8 or higher is required")
        return False
    
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor} detected")
    
    # Install requirements
    if not run_command("pip install -r requirements.txt", "Installing Python packages"):
        return False
    
    # Install Playwright
    if not run_command("playwright install chromium", "Installing Playwright Chromium"):
        return False
    
    # Install spaCy model
    if not run_command("python -m spacy download en_core_web_sm", "Installing spaCy model"):
        print("⚠ spaCy model installation failed - basic analysis will still work")
    
    # Create .env file if it doesn't exist
    env_file = Path(".env")
    if not env_file.exists():
        env_example = Path(".env.example")
        if env_example.exists():
            import shutil
            shutil.copy(env_example, env_file)
            print("✓ Created .env file from template")
            print("⚠ Please edit .env with your Twitter credentials")
        else:
            print("⚠ .env.example not found - creating basic .env file")
            with open(env_file, 'w') as f:
                f.write("""# Twitter Credentials
TWITTER_USERNAME=your_twitter_username
TWITTER_PASSWORD=your_twitter_password

# Other settings will use defaults
""")
            print("✓ Created basic .env file")
            print("⚠ Please edit .env with your Twitter credentials")
    else:
        print("✓ .env file already exists")
    
    # Create data directories
    data_dirs = [
        "data/templates",
        "data/knowledge", 
        "data/logs"
    ]
    
    for dir_path in data_dirs:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
    
    print("✓ Data directories created/verified")
    
    print("\n" + "="*50)
    print("Setup completed successfully!")
    print("="*50)
    print("\nNext steps:")
    print("1. Edit .env with your Twitter credentials")
    print("2. Configure target users in config/targets.py")
    print("3. Run: python main.py")
    print("\n⚠ Important:")
    print("- Use this responsibly and respect Twitter's Terms of Service")
    print("- The safety filters are not foolproof")
    print("- Consider manual review for sensitive content")
    
    return True

if __name__ == "__main__":
    try:
        success = main()
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nSetup cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nSetup failed with error: {e}")
        sys.exit(1)
