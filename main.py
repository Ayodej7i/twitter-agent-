"""
Twitter Controversial Agent - Main Orchestrator
"""

import time
import random
import signal
import sys
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from loguru import logger
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent))

from config.settings import config
from config.targets import targets
from core.improved_monitor import improved_monitor as monitor
from core.improved_monitor import Tweet
from core.analyzer import analyzer, ContentAnalysis
from core.researcher import researcher, ResearchData
from core.generator import generator, ResponseOption
from core.safety import safety_filter, SafetyResult
from utils.scheduler import task_scheduler, response_scheduler

class TwitterAgent:
    """Main Twitter agent orchestrator"""
    
    def __init__(self):
        self.running = False
        self.stats = {
            "tweets_processed": 0,
            "responses_generated": 0,
            "responses_posted": 0,
            "safety_blocks": 0,
            "start_time": None
        }
        
        # Setup logging
        self._setup_logging()
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _setup_logging(self):
        """Setup logging configuration"""
        log_path = Path(config.database.log_path)
        log_path.mkdir(parents=True, exist_ok=True)
        
        logger.add(
            log_path / "agent.log",
            rotation="1 day",
            retention="7 days",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"
        )
        
        logger.info("Twitter Controversial Agent starting up")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down gracefully")
        self.shutdown()
        sys.exit(0)
    
    def start(self):
        """Start the Twitter agent"""
        logger.info("Starting Twitter Controversial Agent")
        
        self.running = True
        self.stats["start_time"] = datetime.now()
        
        # Setup browser and login
        if not self._setup_browser():
            logger.error("Failed to setup browser, exiting")
            return False
        
        # Setup scheduled tasks
        self._setup_scheduled_tasks()
        
        # Main monitoring loop
        self._run_monitoring_loop()
        
        return True
    
    def _setup_browser(self) -> bool:
        """Setup browser and login to Twitter"""
        try:
            logger.info("Setting up browser...")
            
            if not monitor.setup_browser():
                return False
            
            logger.info("Logging into Twitter...")
            if not monitor.login_with_retry():
                return False
            
            logger.info("Browser setup complete")
            return True
            
        except Exception as e:
            logger.error(f"Browser setup failed: {e}")
            return False
    
    def _setup_scheduled_tasks(self):
        """Setup scheduled tasks"""
        
        # Monitoring task
        task_scheduler.add_task(
            name="monitor_tweets",
            function=self.monitor_tweets_task,
            interval=config.twitter.monitor_interval
        )
        
        # Response posting task
        task_scheduler.add_task(
            name="post_responses",
            function=self.post_responses_task,
            interval=60  # Check every minute
        )
        
        # Cleanup task
        task_scheduler.add_task(
            name="cleanup",
            function=self.cleanup_task,
            interval=3600  # Every hour
        )
        
        logger.info("Scheduled tasks setup complete")
    
    def _run_monitoring_loop(self):
        """Main monitoring loop"""
        logger.info("Starting main monitoring loop")
        
        try:
            while self.running:
                # Let the scheduler handle tasks
                time.sleep(10)
                
                # Print stats every 5 minutes
                if datetime.now().minute % 5 == 0:
                    self._print_stats()
                
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
        finally:
            self.shutdown()
    
    def monitor_tweets_task(self):
        """Monitor tweets from target users"""
        try:
            logger.info("Monitoring target users for new tweets")
            
            # Get new tweets
            tweets = monitor.monitor_targets()
            
            if tweets:
                logger.info(f"Found {len(tweets)} new tweets")
                
                for tweet in tweets:
                    self._process_tweet(tweet)
            
            self.stats["tweets_processed"] += len(tweets)
            
        except Exception as e:
            logger.error(f"Error monitoring tweets: {e}")
    
    def _process_tweet(self, tweet: Tweet):
        """Process a single tweet"""
        try:
            logger.info(f"Processing tweet from {tweet.username}: {tweet.content[:50]}...")
            
            # Analyze content
            analysis = analyzer.analyze_tweet(tweet)
            logger.info(f"Analysis: topic={analysis.topic}, sentiment={analysis.sentiment:.2f}, controversy={analysis.controversy_potential:.2f}")
            
            # Research topic
            research_data = researcher.research_topic(analysis, analysis.keywords)
            logger.info(f"Research completed for {analysis.topic}")
            
            # Check if should respond
            if not self._should_respond_to_tweet(tweet, analysis):
                logger.info("Decided not to respond to tweet")
                return
            
            # Generate response options
            response_options = generator.get_multiple_options(tweet, analysis, research_data, count=3)
            logger.info(f"Generated {len(response_options)} response options")
            
            # Filter for safety
            safe_options = safety_filter.filter_response_options(response_options)
            logger.info(f"Safe options: {len(safe_options)}")
            
            if not safe_options:
                logger.warning("No safe response options available")
                self.stats["safety_blocks"] += 1
                return
            
            # Select best response
            best_response = self._select_best_response(safe_options)
            logger.info(f"Selected response: {best_response.text[:50]}...")
            
            # Schedule response
            self._schedule_response(tweet, best_response)
            
            self.stats["responses_generated"] += 1
            
        except Exception as e:
            logger.error(f"Error processing tweet: {e}")
    
    def _should_respond_to_tweet(self, tweet: Tweet, analysis: ContentAnalysis) -> bool:
        """Determine if should respond to a tweet"""
        
        # Check if mentions our username (always respond)
        if tweet.mentions_user:
            return True
        
        # Check rate limits
        if not response_scheduler.check_rate_limits(tweet.username):
            return False
        
        # Check target user preferences
        current_hour = datetime.now().hour
        if not targets.should_respond_to_user(tweet.username, current_hour, 0):
            return False
        
        # Check engagement potential
        if analysis.engagement_prediction < 0.3:
            return False
        
        # Check controversy potential (want some controversy but not too much)
        if analysis.controversy_potential < 0.2:
            return False
        
        # Random factor
        if random.random() < 0.7:  # 70% chance to respond if all checks pass
            return True
        
        return False
    
    def _select_best_response(self, options: List[ResponseOption]) -> ResponseOption:
        """Select the best response from safe options"""
        
        # Sort by engagement prediction, then safety score
        sorted_options = sorted(
            options,
            key=lambda x: (x.engagement_prediction, x.safety_score),
            reverse=True
        )
        
        return sorted_options[0]
    
    def _schedule_response(self, tweet: Tweet, response: ResponseOption):
        """Schedule a response to be posted"""
        
        def post_response():
            try:
                success = monitor.post_response(tweet.url, response.text)
                if success:
                    logger.info(f"Response posted successfully to {tweet.username}")
                    self.stats["responses_posted"] += 1
                    response_scheduler.record_response(tweet.username)
                else:
                    logger.error(f"Failed to post response to {tweet.username}")
            except Exception as e:
                logger.error(f"Error posting response: {e}")
        
        # Schedule with human-like delay
        scheduled_time = response_scheduler.schedule_response(
            tweet.id, tweet.username, post_response
        )
        
        logger.info(f"Response scheduled for {scheduled_time}")
    
    def post_responses_task(self):
        """Post scheduled responses"""
        try:
            ready_responses = response_scheduler.get_pending_responses()
            
            if ready_responses:
                logger.info(f"Posting {len(ready_responses)} ready responses")
                
                for response_data in ready_responses:
                    try:
                        response_data["function"]()
                    except Exception as e:
                        logger.error(f"Error posting response: {e}")
            
        except Exception as e:
            logger.error(f"Error in post responses task: {e}")
    
    def cleanup_task(self):
        """Cleanup old data and logs"""
        try:
            logger.info("Running cleanup task")
            
            # Clean old cache entries
            if hasattr(researcher, 'cache'):
                current_time = datetime.now()
                expired_keys = [
                    key for key, (data, timestamp) in researcher.cache.items()
                    if current_time - timestamp > timedelta(hours=24)
                ]
                
                for key in expired_keys:
                    del researcher.cache[key]
                
                logger.info(f"Cleaned {len(expired_keys)} expired cache entries")
            
            # Clean old response history
            cutoff_time = datetime.now() - timedelta(days=7)
            for key in list(response_scheduler.response_history.keys()):
                response_scheduler.response_history[key] = [
                    timestamp for timestamp in response_scheduler.response_history[key]
                    if timestamp > cutoff_time
                ]
                
                if not response_scheduler.response_history[key]:
                    del response_scheduler.response_history[key]
            
            logger.info("Cleanup completed")
            
        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")
    
    def _print_stats(self):
        """Print current statistics"""
        if not self.stats["start_time"]:
            return
        
        runtime = datetime.now() - self.stats["start_time"]
        
        logger.info(f"""
=== Twitter Agent Statistics ===
Runtime: {runtime}
Tweets Processed: {self.stats["tweets_processed"]}
Responses Generated: {self.stats["responses_generated"]}
Responses Posted: {self.stats["responses_posted"]}
Safety Blocks: {self.stats["safety_blocks"]}
Response Rate: {self.stats["responses_posted"] / max(1, self.stats["tweets_processed"]) * 100:.1f}%
==============================
        """)
    
    def shutdown(self):
        """Shutdown the agent gracefully"""
        logger.info("Shutting down Twitter Controversial Agent")
        
        self.running = False
        
        # Stop scheduler
        task_scheduler.stop_scheduler()
        
        # Close browser
        monitor.close()
        
        # Print final stats
        self._print_stats()
        
        logger.info("Shutdown complete")

def main():
    """Main entry point"""
    agent = TwitterAgent()
    
    try:
        success = agent.start()
        if not success:
            logger.error("Failed to start agent")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
