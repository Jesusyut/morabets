"""
MLB Team Abbreviations Mapping
Official 3-letter abbreviations for all 30 MLB teams
"""

TEAM_ABBREVIATIONS = {
    # American League East
    "Boston Red Sox": "BOS",
    "New York Yankees": "NYY",
    "Tampa Bay Rays": "TB",
    "Toronto Blue Jays": "TOR",
    "Baltimore Orioles": "BAL",
    
    # American League Central
    "Chicago White Sox": "CHW",
    "Cleveland Guardians": "CLE",
    "Detroit Tigers": "DET",
    "Kansas City Royals": "KC",
    "Minnesota Twins": "MIN",
    
    # American League West
    "Houston Astros": "HOU",
    "Los Angeles Angels": "LAA",
    "Oakland Athletics": "OAK",
    "Seattle Mariners": "SEA",
    "Texas Rangers": "TEX",
    
    # National League East
    "Atlanta Braves": "ATL",
    "Miami Marlins": "MIA",
    "New York Mets": "NYM",
    "Philadelphia Phillies": "PHI",
    "Washington Nationals": "WSH",
    
    # National League Central
    "Chicago Cubs": "CHC",
    "Cincinnati Reds": "CIN",
    "Milwaukee Brewers": "MIL",
    "Pittsburgh Pirates": "PIT",
    "St. Louis Cardinals": "STL",
    
    # National League West
    "Arizona Diamondbacks": "ARI",
    "Colorado Rockies": "COL",
    "Los Angeles Dodgers": "LAD",
    "San Diego Padres": "SD",
    "San Francisco Giants": "SF"
}

# Comprehensive team abbreviation mapping including aliases and common variations
TEAM_ABBR_MAP = {
    # Official abbreviations
    "ARI": "ARI", "ARZ": "ARI",  # Arizona Diamondbacks
    "ATL": "ATL",  # Atlanta Braves
    "BAL": "BAL",  # Baltimore Orioles
    "BOS": "BOS",  # Boston Red Sox
    "CHC": "CHC",  # Chicago Cubs
    "CHW": "CHW", "CWS": "CHW",  # Chicago White Sox
    "CIN": "CIN",  # Cincinnati Reds
    "CLE": "CLE",  # Cleveland Guardians
    "COL": "COL",  # Colorado Rockies
    "DET": "DET",  # Detroit Tigers
    "HOU": "HOU",  # Houston Astros
    "KC": "KC", "KCR": "KC", "KCC": "KC",  # Kansas City Royals
    "LAA": "LAA", "ANA": "LAA",  # Los Angeles Angels
    "LAD": "LAD",  # Los Angeles Dodgers
    "MIA": "MIA",  # Miami Marlins
    "MIL": "MIL",  # Milwaukee Brewers
    "MIN": "MIN",  # Minnesota Twins
    "NYM": "NYM",  # New York Mets
    "NYY": "NYY",  # New York Yankees
    "OAK": "OAK",  # Oakland Athletics
    "PHI": "PHI",  # Philadelphia Phillies
    "PIT": "PIT",  # Pittsburgh Pirates
    "SD": "SD", "SDP": "SD",  # San Diego Padres
    "SF": "SF", "SFG": "SF",  # San Francisco Giants
    "SEA": "SEA",  # Seattle Mariners
    "STL": "STL",  # St. Louis Cardinals
    "TB": "TB", "TBR": "TB", "TBD": "TB",  # Tampa Bay Rays
    "TEX": "TEX",  # Texas Rangers
    "TOR": "TOR",  # Toronto Blue Jays
    "WSH": "WSH", "WSN": "WSH",  # Washington Nationals
}

def get_team_abbreviation(full_name):
    """Convert full team name to 3-letter abbreviation"""
    return TEAM_ABBREVIATIONS.get(full_name, full_name[:3].upper())

def normalize_team_abbr(team_abbr):
    """Normalize team abbreviation using comprehensive mapping"""
    if not team_abbr:
        return ""
    return TEAM_ABBR_MAP.get(team_abbr.upper(), team_abbr.upper())

def format_matchup(away_team, home_team):
    """Format matchup using team abbreviations"""
    away_abbr = get_team_abbreviation(away_team)
    home_abbr = get_team_abbreviation(home_team)
    return f"{away_abbr} @ {home_abbr}"
