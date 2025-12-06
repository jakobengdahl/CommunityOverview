"""
Generate a test graph with 500 nodes for performance testing.
This script creates a realistic graph structure with various node types and relationships.
"""

import json
import random
from datetime import datetime, timedelta
from typing import List, Dict

# Node type distributions (should sum to ~500)
NODE_COUNTS = {
    "Actor": 100,
    "Initiative": 150,
    "Legislation": 30,
    "Resource": 80,
    "Capability": 60,
    "Theme": 40,
    "Community": 10,
    "VisualizationView": 0  # Not needed for testing
}

COMMUNITIES = ["eSam", "Myndigheter", "Officiell Statistik"]

# Swedish names and themes for realistic data
ACTOR_NAMES = [
    "Digg", "SKR", "Arbetsförmedlingen", "Skatteverket", "Försäkringskassan",
    "Polismyndigheten", "Domstolsverket", "Bolagsverket", "Lantmäteriet",
    "Trafikverket", "Naturvårdsverket", "Pensionsmyndigheten", "Kronofogden",
    "Migrationsverket", "Tullverket", "Kustbevakningen", "MSB",
    "Socialstyrelsen", "Livsmedelsverket", "Läkemedelsverket"
]

LEGISLATION_KEYWORDS = [
    "NIS2", "GDPR", "OSL", "Dataskyddsförordningen", "E-delegationen",
    "Offentlighets- och sekretesslagen", "Arkivlagen", "Förvaltningslagen",
    "AI-förordningen", "Cybersäkerhetslagen", "Upphandlingslagen"
]

INITIATIVE_THEMES = [
    "Digitalisering", "Cybersäkerhet", "AI", "Cloud", "E-legitimation",
    "Datautbyte", "API-strategi", "Öppna data", "Innovation", "Automatisering",
    "Användarupplevelse", "Tillgänglighet", "Hållbarhet", "Dataanalys",
    "Informationssäkerhet", "Infrastruktur", "Integration", "Standardisering"
]

RESOURCE_TYPES = [
    "Rapport", "Vägledning", "API", "Plattform", "System", "Verktyg",
    "Dataset", "Standard", "Ramverk", "Tjänst", "Portal", "Register"
]

CAPABILITY_AREAS = [
    "Upphandling", "IT-utveckling", "Projektledning", "Informationssäkerhet",
    "Arkitektur", "Datahantering", "Användarforskning", "Systemförvaltning",
    "Testning", "Driftssäkerhet", "Interoperabilitet", "Juridik",
    "Ekonomi", "HR", "Kommunikation", "Innovation"
]

def generate_id(node_type: str, index: int) -> str:
    """Generate unique node ID"""
    prefix = node_type.lower()
    return f"{prefix}_{index}"

def random_date(start_year=2020, end_year=2025) -> str:
    """Generate random date between start and end year"""
    start_date = datetime(start_year, 1, 1)
    end_date = datetime(end_year, 12, 31)
    delta = end_date - start_date
    random_days = random.randint(0, delta.days)
    random_date = start_date + timedelta(days=random_days)
    return random_date.isoformat()

def generate_actors(count: int) -> List[Dict]:
    """Generate Actor nodes"""
    nodes = []
    for i in range(count):
        base_name = random.choice(ACTOR_NAMES) if i < len(ACTOR_NAMES) else f"Myndighet {i}"
        suffix = "" if i < 20 else f" {i-19}"  # Add number suffix for duplicates

        nodes.append({
            "id": generate_id("actor", i + 1),
            "name": f"{base_name}{suffix}",
            "type": "Actor",
            "description": f"Statlig myndighet eller organisation med ansvar för {random.choice(INITIATIVE_THEMES).lower()}",
            "summary": f"Aktör inom {random.choice(['digitalisering', 'förvaltning', 'samhällsservice'])}",
            "communities": random.sample(COMMUNITIES, k=random.randint(1, 2)),
            "metadata": {},
            "created_at": random_date(),
            "updated_at": random_date(2024, 2025)
        })
    return nodes

def generate_legislation(count: int) -> List[Dict]:
    """Generate Legislation nodes"""
    nodes = []
    for i in range(count):
        keyword = random.choice(LEGISLATION_KEYWORDS) if i < len(LEGISLATION_KEYWORDS) else f"Förordning {i}"
        suffix = "" if i < len(LEGISLATION_KEYWORDS) else f" (SFS {2020 + i}:{random.randint(100, 999)})"

        nodes.append({
            "id": generate_id("legislation", i + 1),
            "name": f"{keyword}{suffix}",
            "type": "Legislation",
            "description": f"Lag eller direktiv som reglerar {random.choice(INITIATIVE_THEMES).lower()} i offentlig sektor",
            "summary": f"Krav och riktlinjer för {random.choice(['säkerhet', 'dataskydd', 'tillgänglighet', 'öppenhet'])}",
            "communities": random.sample(COMMUNITIES, k=random.randint(1, 3)),
            "metadata": {"type": random.choice(["EU-direktiv", "Svensk lag", "Förordning"])},
            "created_at": random_date(2018, 2023),
            "updated_at": random_date(2023, 2025)
        })
    return nodes

def generate_initiatives(count: int) -> List[Dict]:
    """Generate Initiative nodes"""
    nodes = []
    for i in range(count):
        theme = random.choice(INITIATIVE_THEMES)
        action = random.choice(["implementering", "utveckling", "införande", "projekt", "program", "satsning"])

        nodes.append({
            "id": generate_id("initiative", i + 1),
            "name": f"{theme}-{action} {i + 1}",
            "type": "Initiative",
            "description": f"Initiativ för att {random.choice(['förbättra', 'modernisera', 'effektivisera', 'säkra'])} {theme.lower()} i offentlig förvaltning",
            "summary": f"Projekt inom {theme.lower()} med {random.randint(2, 10)} deltagande organisationer",
            "communities": random.sample(COMMUNITIES, k=random.randint(1, 2)),
            "metadata": {
                "budget": f"{random.randint(1, 50)} MSEK",
                "duration": f"{random.randint(12, 48)} månader",
                "status": random.choice(["Planering", "Pågående", "Avslutad", "Utvärdering"])
            },
            "created_at": random_date(2020, 2024),
            "updated_at": random_date(2024, 2025)
        })
    return nodes

def generate_resources(count: int) -> List[Dict]:
    """Generate Resource nodes"""
    nodes = []
    for i in range(count):
        resource_type = random.choice(RESOURCE_TYPES)
        theme = random.choice(INITIATIVE_THEMES)

        nodes.append({
            "id": generate_id("resource", i + 1),
            "name": f"{resource_type} för {theme} {i + 1}",
            "type": "Resource",
            "description": f"{resource_type} som stödjer {theme.lower()}-arbete i offentlig sektor",
            "summary": f"{resource_type} med fokus på {random.choice(['samordning', 'vägledning', 'implementering', 'utvärdering'])}",
            "communities": random.sample(COMMUNITIES, k=random.randint(1, 2)),
            "metadata": {
                "format": random.choice(["PDF", "Web", "API", "Software", "Dataset"]),
                "version": f"{random.randint(1, 5)}.{random.randint(0, 9)}"
            },
            "created_at": random_date(2020, 2024),
            "updated_at": random_date(2024, 2025)
        })
    return nodes

def generate_capabilities(count: int) -> List[Dict]:
    """Generate Capability nodes"""
    nodes = []
    for i in range(count):
        capability = random.choice(CAPABILITY_AREAS)

        nodes.append({
            "id": generate_id("capability", i + 1),
            "name": f"{capability} {i + 1}",
            "type": "Capability",
            "description": f"Förmåga inom {capability.lower()} för offentlig sektor",
            "summary": f"Kompetens och resurser för {capability.lower()}",
            "communities": random.sample(COMMUNITIES, k=random.randint(1, 2)),
            "metadata": {
                "level": random.choice(["Grundläggande", "Avancerad", "Expert"]),
                "area": capability
            },
            "created_at": random_date(2020, 2023),
            "updated_at": random_date(2023, 2025)
        })
    return nodes

def generate_themes(count: int) -> List[Dict]:
    """Generate Theme nodes"""
    nodes = []
    for i in range(count):
        theme = INITIATIVE_THEMES[i] if i < len(INITIATIVE_THEMES) else f"Tema {i}"

        nodes.append({
            "id": generate_id("theme", i + 1),
            "name": theme,
            "type": "Theme",
            "description": f"Strategiskt fokusområde: {theme}",
            "summary": f"Övergripande tema för samordning och utveckling",
            "communities": COMMUNITIES,  # Themes are cross-community
            "metadata": {
                "priority": random.choice(["Hög", "Medel", "Låg"]),
                "category": random.choice(["Teknologi", "Process", "Styrning", "Kompetens"])
            },
            "created_at": random_date(2019, 2022),
            "updated_at": random_date(2023, 2025)
        })
    return nodes

def generate_communities(count: int) -> List[Dict]:
    """Generate Community nodes"""
    nodes = []
    for i in range(count):
        community_name = COMMUNITIES[i] if i < len(COMMUNITIES) else f"Community {i}"

        descriptions = {
            "eSam": "Samverkan för digital utveckling i offentlig sektor",
            "Myndigheter": "Nätverk av statliga myndigheter",
            "Officiell Statistik": "System för officiell statistik"
        }

        nodes.append({
            "id": generate_id("community", i + 1),
            "name": community_name,
            "type": "Community",
            "description": descriptions.get(community_name, f"Community för samverkan kring {community_name}"),
            "summary": f"Samverkansplattform med {random.randint(10, 100)} medlemmar",
            "communities": [community_name],
            "metadata": {
                "members": random.randint(10, 100),
                "founded": random.randint(2000, 2020)
            },
            "created_at": random_date(2015, 2020),
            "updated_at": random_date(2024, 2025)
        })
    return nodes

def generate_edges(nodes: List[Dict]) -> List[Dict]:
    """Generate realistic edges between nodes"""
    edges = []
    edge_id = 1

    # Get node indices by type
    actors = [n for n in nodes if n["type"] == "Actor"]
    initiatives = [n for n in nodes if n["type"] == "Initiative"]
    legislation = [n for n in nodes if n["type"] == "Legislation"]
    resources = [n for n in nodes if n["type"] == "Resource"]
    capabilities = [n for n in nodes if n["type"] == "Capability"]
    themes = [n for n in nodes if n["type"] == "Theme"]
    communities = [n for n in nodes if n["type"] == "Community"]

    # Actors BELONGS_TO Communities
    for actor in actors:
        for comm_name in actor["communities"]:
            comm = next((c for c in communities if c["name"] == comm_name), None)
            if comm:
                edges.append({
                    "id": f"edge_{edge_id}",
                    "source": actor["id"],
                    "target": comm["id"],
                    "type": "BELONGS_TO"
                })
                edge_id += 1

    # Initiatives BELONGS_TO Actors
    for initiative in initiatives:
        actor = random.choice(actors)
        edges.append({
            "id": f"edge_{edge_id}",
            "source": initiative["id"],
            "target": actor["id"],
            "type": "BELONGS_TO"
        })
        edge_id += 1

    # Initiatives IMPLEMENTS Legislation (some)
    for initiative in random.sample(initiatives, min(100, len(initiatives))):
        leg = random.choice(legislation)
        edges.append({
            "id": f"edge_{edge_id}",
            "source": initiative["id"],
            "target": leg["id"],
            "type": "IMPLEMENTS"
        })
        edge_id += 1

    # Initiatives PRODUCES Resources
    for resource in resources:
        initiative = random.choice(initiatives)
        edges.append({
            "id": f"edge_{edge_id}",
            "source": initiative["id"],
            "target": resource["id"],
            "type": "PRODUCES"
        })
        edge_id += 1

    # Initiatives PRODUCES Capabilities (some)
    for capability in random.sample(capabilities, min(40, len(capabilities))):
        initiative = random.choice(initiatives)
        edges.append({
            "id": f"edge_{edge_id}",
            "source": initiative["id"],
            "target": capability["id"],
            "type": "PRODUCES"
        })
        edge_id += 1

    # Initiatives RELATES_TO Themes
    for initiative in initiatives:
        # Each initiative relates to 1-3 themes
        for theme in random.sample(themes, k=random.randint(1, min(3, len(themes)))):
            edges.append({
                "id": f"edge_{edge_id}",
                "source": initiative["id"],
                "target": theme["id"],
                "type": "RELATES_TO"
            })
            edge_id += 1

    # Resources RELATES_TO Resources (some cross-references)
    for _ in range(30):
        source = random.choice(resources)
        target = random.choice([r for r in resources if r["id"] != source["id"]])
        edges.append({
            "id": f"edge_{edge_id}",
            "source": source["id"],
            "target": target["id"],
            "type": "RELATES_TO"
        })
        edge_id += 1

    return edges

def main():
    """Generate and save test graph"""
    print("Generating test graph with 500+ nodes...")

    nodes = []

    # Generate all node types
    nodes.extend(generate_actors(NODE_COUNTS["Actor"]))
    nodes.extend(generate_legislation(NODE_COUNTS["Legislation"]))
    nodes.extend(generate_initiatives(NODE_COUNTS["Initiative"]))
    nodes.extend(generate_resources(NODE_COUNTS["Resource"]))
    nodes.extend(generate_capabilities(NODE_COUNTS["Capability"]))
    nodes.extend(generate_themes(NODE_COUNTS["Theme"]))
    nodes.extend(generate_communities(NODE_COUNTS["Community"]))

    print(f"Generated {len(nodes)} nodes")

    # Generate edges
    edges = generate_edges(nodes)
    print(f"Generated {len(edges)} edges")

    # Create graph structure
    graph = {
        "nodes": nodes,
        "edges": edges
    }

    # Save to file
    output_file = "graph_test_500.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)

    print(f"✅ Test graph saved to {output_file}")
    print(f"   Nodes: {len(nodes)}")
    print(f"   Edges: {len(edges)}")
    print(f"   Node types distribution:")
    for node_type, count in NODE_COUNTS.items():
        if count > 0:
            print(f"     - {node_type}: {count}")

if __name__ == "__main__":
    main()
