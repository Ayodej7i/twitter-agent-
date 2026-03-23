"""
Fan Profile Setup Questionnaire
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent))

from config.fan_profile import fan_profile, FanProfile

def setup_fan_profile():
    """Interactive setup for fan profile"""
    
    print("⚽ Fan Profile Setup")
    print("=" * 50)
    print("Help me understand your preferences so the agent can respond")
    print("in a way that matches your personality and views!")
    print("=" * 50)
    
    questions = fan_profile.get_personalized_questions()
    responses = {}
    
    for i, question in enumerate(questions, 1):
        print(f"\nQ{i}: {question}")
        
        if "scale" in question.lower():
            try:
                response = input("Your answer (1-10): ")
                responses[f"question_{i}"] = int(response)
            except ValueError:
                responses[f"question_{i}"] = 7  # Default to passionate
        elif "realistic" in question.lower() and "optimistic" in question.lower():
            response = input("Your answer (realistic/optimistic): ").lower()
            responses[f"question_{i}"] = response
        elif "rival" in question.lower():
            response = input("Your answer: ")
            responses[f"question_{i}"] = response
        elif "comfortable" in question.lower() and "banter" in question.lower():
            response = input("Your answer (yes/no): ").lower()
            responses[f"question_{i}"] = response
        elif "aggressively" in question.lower() and "moderately" in question.lower():
            response = input("Your answer (aggressively/moderately): ").lower()
            responses[f"question_{i}"] = response
        elif "plays poorly" in question.lower():
            response = input("Your answer (yes/no): ").lower()
            responses[f"question_{i}"] = response
        elif "historical" in question.lower() and "current" in question.lower():
            response = input("Your answer (historical/current): ").lower()
            responses[f"question_{i}"] = response
        elif "jokes" in question.lower() and "rival" in question.lower():
            response = input("Your answer (yes/no): ").lower()
            responses[f"question_{i}"] = response
        else:
            response = input("Your answer: ")
            responses[f"question_{i}"] = response
    
    # Update fan profile based on responses
    profile_updates = {}
    
    # Parse responses and update profile
    for key, value in responses.items():
        if "scale" in key:
            passion_level = value
            if passion_level >= 8:
                profile_updates["fan_personality"] = "passionate"
            elif passion_level >= 6:
                profile_updates["fan_personality"] = "supportive"
            else:
                profile_updates["fan_personality"] = "casual"
        
        elif "realistic" in key:
            if value == "realistic":
                profile_updates["preferred_narratives"] = ["realistic_assessment", "acknowledge_issues"]
            else:
                profile_updates["preferred_narratives"] = ["optimistic_outlook", "future_focus"]
        
        elif "rival" in key and value:
            # Add to rival teams if not already there
            if value.lower() not in [r.lower() for r in fan_profile.profile.rival_teams]:
                fan_profile.profile.rival_teams.append(value)
    
    # Apply updates
    if profile_updates:
        fan_profile.update_profile(**profile_updates)
        print("\n✅ Fan profile updated!")
    else:
        print("\n⚠️  No profile updates needed")
    
    # Show current profile
    print("\n📋 Your Current Fan Profile:")
    print("-" * 30)
    print(f"Favorite Team: {fan_profile.profile.favorite_team}")
    print(f"Fan Personality: {fan_profile.profile.fan_personality}")
    print(f"Rival Teams: {', '.join(fan_profile.profile.rival_teams)}")
    print(f"Preferred Narratives: {', '.join(fan_profile.profile.preferred_narratives)}")
    
    # Generate example responses
    print("\n💬 Example Responses Based on Your Profile:")
    print("-" * 50)
    
    examples = [
        "Someone criticizes your team's current form",
        "Liverpool fan boasts about their recent success", 
        "Someone says Everton are better than United",
        "Question about United's transfer strategy"
    ]
    
    for example in examples:
        print(f"\nScenario: {example}")
        guidance = fan_profile.get_response_guidance("@example_user", "your team")
        print(f"Recommended Stance: {guidance['stance']}")
        print(f"Preferred Angles: {', '.join(guidance['preferred_angles'][:2])}")
    
    print("\n🎉 Setup Complete!")
    print("The agent will now generate responses that match your fan personality!")
    print("\nTo run the agent: python main.py")
    print("To see demo: python demo_mode.py")

if __name__ == "__main__":
    setup_fan_profile()
