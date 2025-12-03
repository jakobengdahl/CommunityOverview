"""
Test-script för att verifiera graf-funktionalitet
Kör: python test_graph.py
"""

from graph_storage import GraphStorage
from models import Node, Edge, NodeType, RelationshipType

def test_basic_operations():
    """Testar grundläggande CRUD-operationer"""
    print("=== Test 1: Ladda graf ===")
    graph = GraphStorage("graph.json")
    print(f"✓ Laddade {len(graph.nodes)} noder och {len(graph.edges)} edges")

    print("\n=== Test 2: Sök efter NIS2 ===")
    results = graph.search_nodes("NIS2", communities=["eSam"])
    print(f"✓ Hittade {len(results)} noder:")
    for node in results:
        print(f"  - {node.type.value}: {node.name}")

    print("\n=== Test 3: Hämta relaterade noder ===")
    # Hitta NIS2 Implementeringsprojekt
    nis2_project = next((n for n in graph.nodes.values() if "NIS2 Implementering" in n.name), None)
    if nis2_project:
        related = graph.get_related_nodes(nis2_project.id, depth=1)
        print(f"✓ Nod '{nis2_project.name}' har {len(related['nodes'])-1} relaterade noder:")
        for node in related['nodes']:
            if node.id != nis2_project.id:
                print(f"  - {node.type.value}: {node.name}")

    print("\n=== Test 4: Hitta liknande noder ===")
    similar = graph.find_similar_nodes("Cybersäkerhet", threshold=0.6)
    print(f"✓ Hittade {len(similar)} liknande noder:")
    for s in similar:
        print(f"  - {s.node.name} (similarity: {s.similarity_score})")

    print("\n=== Test 5: Statistik ===")
    stats = graph.get_stats(communities=["eSam"])
    print(f"✓ Graf-statistik för eSam:")
    print(f"  - Totalt antal noder: {stats.total_nodes}")
    print(f"  - Totalt antal edges: {stats.total_edges}")
    print(f"  - Noder per typ:")
    for node_type, count in stats.nodes_by_type.items():
        print(f"    - {node_type}: {count}")

    print("\n=== Test 6: Lägg till och ta bort nod ===")
    test_node = Node(
        type=NodeType.INITIATIVE,
        name="Test-projekt",
        description="Ett testprojekt för verifiering",
        summary="Test",
        communities=["eSam"]
    )

    result = graph.add_nodes([test_node], [])
    if result.success:
        print(f"✓ Lade till testnod: {test_node.id}")

        # Ta bort testnoden
        delete_result = graph.delete_nodes([test_node.id], confirmed=True)
        if delete_result.success:
            print(f"✓ Tog bort testnod")
        else:
            print(f"✗ Kunde inte ta bort: {delete_result.message}")
    else:
        print(f"✗ Kunde inte lägga till: {result.message}")

    print("\n=== Alla tester klara! ===")

if __name__ == "__main__":
    test_basic_operations()
