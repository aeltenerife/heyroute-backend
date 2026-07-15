import re
import random as rd

# List of known toll roads in Metro Manila
# Used to identify "Avoid Toll" intents into ORS avoid_features
toll_roads = [
    "Skyway",
    "North Luzon Expressway",
    "South Luzon Expressway",
    "NAIA Expressway",
    "Manila-Cavite Expressway",
    "NLEX Harbor Link",
    "Cavite-Laguna Expressway",
]

# Dictionary mapping standard road names to their common aliases
ROAD_ALIASES = {
    "EDSA": ["EDSA", "Epifanio de los Santos Avenue", "Epifanio de los Santos"],

    "Skyway": ["Skyway", "Metro Manila Skyway", "Metro Manila Skyway System Stage 3"],
    "North Luzon Expressway": ["North Luzon Expressway", "NLEX"],
    "South Luzon Expressway": ["South Luzon Expressway", "SLEX"],
    "NAIA Expressway": ["NAIA Expressway", "NAIAX"],
    "Manila-Cavite Expressway": ["Manila-Cavite Expressway", "Cavite Expressway", "CAVITEX"],
    "NLEX Harbor Link": ["NLEX Harbor Link", "Harbor Link"],
    "Cavite-Laguna Expressway": ["Cavite-Laguna Expressway", "CALAX"],

    "Carlos P. Garcia Avenue": [
        "Carlos P. Garcia Avenue", "Carlos P. Garcia", "CP Garcia",
        "CP Garcia Avenue", "CP Garcia Ave"
    ],

    "Taft Avenue": ["Taft Avenue", "Taft Ave", "Taft"],
    "Ortigas Avenue": ["Ortigas Avenue", "Ortigas Ave", "Ortigas"],
    "Commonwealth Avenue": ["Commonwealth Avenue", "Commonwealth Ave", "Commonwealth"],
    "North Avenue": ["North Avenue", "North Ave"],
    "Quezon Avenue": ["Quezon Avenue", "Quezon Ave"],
    "East Avenue": ["East Avenue", "East Ave"],
    "Kalayaan Avenue": ["Kalayaan Avenue", "Kalayaan Ave", "Kalayaan"],
    "Visayas Avenue": ["Visayas Avenue", "Visayas Ave", "Visayas"],
    "Katipunan Avenue": ["Katipunan Avenue", "Katipunan Ave", "Katipunan"],

    "E. Rodriguez Jr. Avenue": [
        "E. Rodriguez Jr. Avenue", "E. Rodriguez Jr. Ave", "E. Rodriguez Jr.",
        "E. Rodriguez Jr Avenue", "E. Rodriguez Jr Ave", "E. Rodriguez Jr",
        "E Rodriguez Jr. Avenue", "E Rodriguez Jr. Ave", "E Rodriguez Jr.",
        "E Rodriguez Jr Avenue", "E Rodriguez Jr Ave", "E Rodriguez Jr",
        "E. Rodriguez Avenue", "E. Rodriguez Ave", "E. Rodriguez", "E. Rod",
        "E Rodriguez Avenue", "E Rodriguez Ave", "E Rodriguez", "E Rod"
    ],

    "Roxas Boulevard": ["Roxas Boulevard", "Roxas Blvd", "Roxas"],
    "Shaw Boulevard": ["Shaw Boulevard", "Shaw Blvd", "Shaw"],
    "España Boulevard": ["España Boulevard", "Espana Blvd", "España", "Espana"],
    "Aurora Boulevard": ["Aurora Boulevard", "Aurora Blvd", "Aurora"],

    "Gregorio Araneta Avenue": [
        "Gregorio Araneta Avenue", "G. Araneta Avenue", "G Araneta Avenue",
        "G. Araneta Ave", "G Araneta Ave",
        "G. Araneta", "G Araneta"
    ],
    "Osmeña Highway": ["Osmeña Highway", "Osmena Highway", "Osmeña", "Osmena"],
    "Quirino Avenue": ["Quirino Avenue", "Quirino Ave", "Quirino"],
    "C. M. Recto Avenue": ["C. M. Recto Avenue", "C.M. Recto Avenue", "CM Recto Avenue", "Recto Avenue", "Recto Ave", "Recto"],
    "A. H. Lacson Avenue": ["Lacson Avenue", "Lacson Ave", "Lacson"],

    "Marcos Highway": ["Marcos Highway", "Marcos Hwy"],
    "Sumulong Highway": ["Sumulong Highway", "Sumulong Hwy"],
    "Quirino Highway": ["Quirino Highway", "Quirino Hwy"],
    "Luzon Avenue": ["Luzon Avenue", "Luzon Ave"],
    "Mindanao Avenue": ["Mindanao Avenue", "Mindanao Ave", "Mindanao"],
    "Tandang Sora Avenue": ["Tandang Sora Avenue", "Tandang Sora"],
    "Congressional Avenue": ["Congressional Avenue", "Congressional Ave", "Congressional"],

    "Senator Gil J. Puyat Avenue": ["Gil Puyat Avenue", "Gil Puyat", "Buendia Avenue", "Buendia"],
    "Ayala Avenue": ["Ayala Avenue", "Ayala Ave", "Ayala"],
    "McKinley Road": ["McKinley Road", "McKinley Rd"],
    "Pasig Boulevard": ["Pasig Boulevard", "Pasig Blvd", "Pasig"],

    "Alabang-Zapote Road": ["Alabang-Zapote Road", "Alabang Zapote Road", "Alabang Zapote"],
    "Boni Avenue": ["Boni Avenue", "Boni Ave", "Boni"],
    "Pioneer Street": ["Pioneer Street", "Pioneer"],
}

async def normalize_road_name(road_name: str) -> str:
    """
    Normalizes a road name using predefined aliases.

    Example:
        Input: "edsa"
        Output: "EDSA"
    
    To ensure consistency when matching user input with known roads.

    Parameters:
        road_name (str): Raw road name input.

    Returns:
        str: The normalized road name.
    """

    road_upper = road_name.strip().upper()
    for standard_name, aliases in ROAD_ALIASES.items():
        if road_upper in [a.upper() for a in aliases]:
            return standard_name
    return road_name.strip()

async def check_label_role(text: str, label: str):
    """
    Checks if a given label (road name) is mentioned in the text as an origin or destination.

    Parameters:
        text (str): Input string.
        label (str): The road name to check for.

    Returns:
        bool: True if the label is found as an origin or destination, False otherwise.
    """

    # Pattern 1: Origin
    is_origin = any([
        re.search(rf"\bfrom\s+{label}\b", text), # "from home"
        re.search(rf"\b{label}\s+to\b", text),   # "home to work"
        re.search(rf"\b{label}\s+as\s+origin\b", text)
    ])

    # Pattern 2: Destination
    is_destination = any([
        re.search(rf"\bto\s+(?:the\s+)?{label}\b", text),       # "to work"
        re.search(rf"\btake\s+me\s+(?:to\s+)?(?:the\s+)?{label}\b", text), # "take me home"
        re.search(rf"\bgo\s+to\s+(?:the\s+)?{label}\b", text),
        re.search(rf"\b{label}$", text)               # safer endswith
    ])

    return is_origin, is_destination

async def resolve_collisions(intents):
    """
    Resolves conflicts between different intents based on predefined rules.

    Parameters:
        intents (dict): A dictionary of detected intents with boolean values.

    Returns:
        dict: A dictionary of resolved intents.
    """

    # Pattern 1 & 3: If anything is unclear, Clarification wins.
    if intents.get("clarifications") and (intents.get("generate_routes") or intents.get("trip_changes")):
        return {k: (k == "clarifications") for k in intents}

    # Pattern 2: Start Nav vs Generate Routes
    elif intents.get("start_nav") and intents.get("generate_routes"):
        return {k: (k == "generate_routes") for k in intents}
    
    # Pattern 4: Clarification vs Start Nav
    elif intents.get("clarifications") and intents.get("start_nav"):
        return {k: (k == "clarifications") for k in intents}
        
    return intents

async def build_gpt_prompt(system_content, specific_content, user_content):
    """
    Constructs a structured prompt for GPT model based on the provided system, specific, and user content.

    Parameters:
        system_content (str): General instructions or context for the GPT model.
        
        specific_content (str): Specific instructions or constraints
        user_content (str): User input or conversation context.

    Returns:
        list: A list of dictionaries representing the structured prompt for the GPT model.
    """

    prompt = [
        {"role": "system", "content": system_content},
        {"role": "system", "content": specific_content},
        {"role": "user", "content": user_content}
    ]
    return prompt

async def format_heyroute_response(summary):
    """
    Formats a HeyRoute response based on the provided summary of the route.

    Parameters:
        summary (dict): A dictionary containing route details such as origin, destination, via, distance, duration, and option.

    Returns:
        str: A formatted response string describing the route to the user.
    """

    # Extract data with fallbacks
    orig = summary.get("origin")
    orig = orig if orig != "current location" else "your current location"
    dest = summary.get("destination")
    via = summary.get("via")
    option = summary.get("option")
    route_phrase = f"{option} route" if option != "recommended" else "route"
    
    via_phrase = ""
    if via:
        via_clean = via.replace("via ", "")
        via_phrase = f" via {via_clean}"

    return (
        f"I've found a {route_phrase} to {dest} from {orig}{via_phrase}. "
        f"Would you like to start navigation now? Say 'Let's go' to begin."
    )

async def format_alternates_response(summaries):
    """
    Formats a response presenting multiple route options to the user based on the provided summaries.

    Parameters:
        summaries (list): A list of dictionaries, each containing route details such as origin, destination, via,
        distance, duration, and option.

    Returns:
        str: A formatted response string describing the route options to the user.
    """

    # Start the response
    intros = [
        "Sure! I found a few different ways to get there. ",
        "I've found a few options for you. ",
        "Here are the different ways we can go: ",
        "Okay, looking at the map, we have a few choices. "
    ]
    
    route_descriptions = []
    for r in summaries:
        # Clean the 'via' text
        via_clean = r['via'].replace("via ", "")
        desc = f"Route {r['index']} goes via {via_clean}."
        route_descriptions.append(desc)
    body = "\n".join(route_descriptions)
    
    # The call to action
    outros = [
        "\n\nWhich one of these sounds best to you?",
        "\n\nDo any of these routes look good to you?",
        "\n\nLet me know which one you'd like to take.",
        "\n\nWhich route do you want to go with?"
    ]

    intro = rd.choice(intros)
    outro = rd.choice(outros)
    
    response = f"{intro}\n{body}\n{outro}"
    
    return response

def extract_json(raw_text: str) -> str:
    """
    Extracts a JSON string from an LLM response that may contain
    markdown code fences or extra surrounding text.
    
    Handles:
      - ```json { ... } ```
      - ``` { ... } ```
      - Raw JSON: { ... }
      - JSON with leading/trailing text
      - Python-style booleans (True/False)
    
    Returns the extracted JSON string, or the original text if no JSON found.
    """
    extracted = raw_text.strip()
    
    # 1. Try to extract from markdown code fences
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', raw_text, re.DOTALL)
    if fence_match:
        extracted = fence_match.group(1).strip()
    else:
        # 2. Try to find a JSON object directly
        brace_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if brace_match:
            extracted = brace_match.group(0).strip()
    
    # 3. Fix Python-style booleans → JSON-style
    extracted = re.sub(r'\bTrue\b', 'true', extracted)
    extracted = re.sub(r'\bFalse\b', 'false', extracted)
    
    return extracted
