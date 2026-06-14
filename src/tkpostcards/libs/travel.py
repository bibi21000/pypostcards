# -*- encoding: utf-8 -*-
from math import radians, sin, cos, sqrt, atan2

from ortools.constraint_solver import pywrapcp
from ortools.constraint_solver import routing_enums_pb2


class ParcoursCartes:
    """
    Optimisation d'un parcours ouvert :
    départ -> cartes -> arrivée libre

    Ne revient pas au point de départ.
    """

    def __init__(self, cartes):
        self.cartes = cartes

    @staticmethod
    def haversine(lat1, lon1, lat2, lon2):
        """Distance en mètres."""
        R = 6371000

        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)

        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1))
            * cos(radians(lat2))
            * sin(dlon / 2) ** 2
        )

        return 2 * R * atan2(sqrt(a), sqrt(1 - a))

    def _filtrer_cartes(self, collection=None):
        resultat = []

        for carte in self.cartes:

            if collection is not None:
                if collection not in carte.get("collections", []):
                    continue

            coord = carte.get("coord")

            if (
                coord is None
                or len(coord) != 2
                or coord[0] is None
                or coord[1] is None
            ):
                continue

            resultat.append(carte)

        return resultat

    def calculer(
        self,
        latitude,
        longitude,
        collection=None,
        time_limit=10,
    ):
        cartes = self._filtrer_cartes(collection)

        if not cartes:
            return {
                "distance_m": 0,
                "distance_km": 0.0,
                "start": (latitude, longitude),
                "end": None,
                "cards": [],
            }

        # ------------------------------------------------------------------
        # Noeud 0 = point de départ utilisateur
        # Noeuds 1..N = cartes
        # Noeud N+1 = fin fictive
        # ------------------------------------------------------------------

        coords = [(latitude, longitude)]

        for carte in cartes:
            coords.append(tuple(carte["coord"]))

        nb_cartes = len(cartes)

        start_node = 0
        end_node = nb_cartes + 1

        total_nodes = nb_cartes + 2

        matrix = [
            [0 for _ in range(total_nodes)]
            for _ in range(total_nodes)
        ]

        # Distances réelles entre départ et cartes
        for i in range(nb_cartes + 1):
            for j in range(nb_cartes + 1):

                if i == j:
                    continue

                matrix[i][j] = int(
                    self.haversine(
                        coords[i][0],
                        coords[i][1],
                        coords[j][0],
                        coords[j][1],
                    )
                )

        # Coût nul vers la fin fictive
        for i in range(nb_cartes + 1):
            matrix[i][end_node] = 0

        # La fin fictive ne repart jamais
        for j in range(total_nodes):
            matrix[end_node][j] = 0

        manager = pywrapcp.RoutingIndexManager(
            total_nodes,
            1,
            [start_node],
            [end_node],
        )

        routing = pywrapcp.RoutingModel(manager)

        def distance_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)

            return matrix[from_node][to_node]

        transit_callback = routing.RegisterTransitCallback(
            distance_callback
        )

        routing.SetArcCostEvaluatorOfAllVehicles(
            transit_callback
        )

        params = pywrapcp.DefaultRoutingSearchParameters()

        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )

        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )

        params.time_limit.seconds = time_limit

        solution = routing.SolveWithParameters(params)

        if solution is None:
            raise RuntimeError(
                "Impossible de trouver un parcours."
            )

        ordre = []
        distance_totale = 0

        index = routing.Start(0)

        derniere_carte = None

        while not routing.IsEnd(index):

            current_node = manager.IndexToNode(index)

            next_index = solution.Value(
                routing.NextVar(index)
            )

            next_node = manager.IndexToNode(next_index)

            # On ne compte pas le saut vers la fin fictive
            if next_node != end_node:
                distance_totale += matrix[current_node][next_node]

            if current_node > 0:
                carte = cartes[current_node - 1]
                # ~ ordre.append(carte)
                ordre.append({
                    "id" : carte['id'],
                    "title" : carte['title']
                })
                derniere_carte = carte

            index = next_index

        return {
            "distance_m": distance_totale,
            "distance_km": round(distance_totale / 1000, 2),
            "start": (latitude, longitude),
            "end": (
                tuple(derniere_carte["coord"])
                if derniere_carte
                else None
            ),
            # ~ "carte_arrivee": derniere_carte,
            "count": len(ordre),
            "cards": ordre,
        }
