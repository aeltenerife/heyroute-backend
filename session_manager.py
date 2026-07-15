"""
Manages user sessions across the HeyRoute application.
"""
class SessionState:
    def __init__(self):
        self.conversation_history = []
        self.turn_count = 0
        self.routes_data = []
        self.current_route_params = {}
        self.final_gpt_response = None
        self.primary_route = None
        self.route_created = False
        self.pending_preference = True
        self.current_location = None
        self.navigation_started = False
        self.semantic_context = {'origin_label': None, 'destination_label': None, 'origin_known': False, 'destination_known': False, 'origin_value': None, 'destination_value': None}

    def increment_turn(self):
        self.turn_count += 1
        return self.turn_count

    def clear_trip_context(self):
        self.conversation_history = []
        self.turn_count = 0
        self.routes_data = []
        self.current_route_params = {}
        self.final_gpt_response = None
        self.primary_route = None
        self.route_created = False
        self.pending_preference = True
        self.navigation_started = False
        self.semantic_context = {'origin_label': None, 'destination_label': None, 'origin_known': False, 'destination_known': False, 'origin_value': None, 'destination_value': None}

SESSIONS = {}
