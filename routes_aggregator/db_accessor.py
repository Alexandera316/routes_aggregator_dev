import itertools

from neo4j.v1 import GraphDatabase, basic_auth, CypherError, DatabaseError

from routes_aggregator.model import Entity, Station, Route, RoutePoint, Path, PathItem
from routes_aggregator.utils import time_to_minutes


class MatchByParametersQueryGenerator:

    MATCH_PART = "MATCH (n:{label}) WHERE "
    RETURN_PART = "RETURN DISTINCT n LIMIT $limit"

    QUERY_PATTERN_MAP = {
        "STARTS_WITH": "LOWER(n.{}) STARTS WITH LOWER({})",
        "STRICT": "n.{} = {}",
        "REGEX": "n.{} =~ {}"
    }

    def __init__(self):
        pass

    def generate_query(self, label, search_mode, property_names, property_values):

        pattern = self.QUERY_PATTERN_MAP.get(search_mode.upper())
        if not pattern:
            return ''

        conditions = ' OR '.join(
            map(
                lambda item: pattern.format(item[0], repr(item[1])),
                itertools.product(property_names, property_values)
            )
        )

        return self.MATCH_PART.format(label=label) + conditions + self.RETURN_PART


class MatchPathsWithSingleRouteQueryGenerator:

    MATCH_PART_PATTERN = "(s{id}:Station)-[r{id}:ROUTE_CONNECTION]->(n:Route)"
    WHERE_PART_PATTERN = "s{id}.domain_id in $station_ids_{id} "
    CONDITION_PART_PATTERN = "toInteger(r{}.station_number) < toInteger(r{}.station_number) "

    def __init__(self):
        pass

    def generate_query(self, station_ids):

        stations_count = len(station_ids)

        match_part = "MATCH "
        where_part = " WHERE "
        condition_part = ""
        return_part = "RETURN DISTINCT r1, n, r{} LIMIT $limit".format(stations_count)

        for i, ids in enumerate(station_ids):
            match_part += self.MATCH_PART_PATTERN.format(id=i + 1)

            if station_ids[i]:
                where_part += self.WHERE_PART_PATTERN.format(id=i + 1)
                if i + 1 < stations_count:
                    where_part += "and "

            if i + 1 < stations_count:
                match_part += ", "
            if i > 0:
                condition_part += "and " + self.CONDITION_PART_PATTERN.format(i, i + 1)

        return match_part + where_part + condition_part + return_part


class MatchPathsWithMultipleRoutesQueryGenerator:

    MATCH_PART_BEGIN = "MATCH (s1:Station)-[r1:ROUTE_CONNECTION]->(n1:Route)"
    MATCH_PART_END = "<-[r{}:ROUTE_CONNECTION]-(s{}:Station) "
    MATCH_PART_PATTERN = "<-[r{}:ROUTE_CONNECTION]-(s{}:Station)-[r{}:ROUTE_CONNECTION]->(n{}:Route)"

    WHERE_PART_BEGIN = "WHERE s1.domain_id in $station_ids_1 "
    WHERE_PART_PATTERN = "and s{id}.domain_id in $station_ids_{id} "
    CONDITION_PART = "and toInteger(r{}.station_number) < toInteger(r{}.station_number) "

    RETURN_PART_BEGIN = "RETURN DISTINCT r1, n1, r2"
    RETURN_PART_END = " LIMIT $limit"
    RETURN_PART_PATTERN = ", r{}, n{}, r{}"

    def __init__(self):
        pass

    def generate_query(self, station_ids):
        transfers_count = len(station_ids) - 2

        match_part = self.MATCH_PART_BEGIN
        where_part = self.WHERE_PART_BEGIN + self.WHERE_PART_PATTERN.format(id=transfers_count + 2)
        condition_part = self.CONDITION_PART.format(1, 2)
        return_part = self.RETURN_PART_BEGIN

        for i in range(transfers_count):
            match_part += self.MATCH_PART_PATTERN.format(2 * i + 2, i + 2, 2 * i + 3, i + 2)
            condition_part += self.CONDITION_PART.format(2 * i + 3, 2 * i + 4)
            if station_ids[i + 1]:
                where_part += self.WHERE_PART_PATTERN.format(id=i + 2)
            return_part += self.RETURN_PART_PATTERN.format(2 * i + 3, i + 2, 2 * i + 4)

        match_part += self.MATCH_PART_END.format(2 * transfers_count + 2, transfers_count + 2)
        return_part += self.RETURN_PART_END

        return match_part + where_part + condition_part + return_part


class DbAccessor:

    CREATE_NODE = r"CREATE (n:{label}  {{ {properties} }} ) RETURN n"
    CREATE_ROUTE_CONNECTION = \
        "MATCH (a:Route {{ domain_id: '{route_domain_id}' }}), " \
        "      (b:Station {{ domain_id: '{station_domain_id}' }}) " \
        "CREATE (a)<-[:ROUTE_CONNECTION {{ {properties} }} ]-(b)"
    CREATE_TRANSITION = \
        "MATCH (a:Station {{ domain_id: '{from_domain_id}' }}), " \
        "      (b:Station {{ domain_id: '{to_domain_id}' }}) " \
        "CREATE (a)-[:TRANSITION {{ {properties} }} ]->(b)"

    CREATE_INDEX = "CREATE INDEX ON :{label}({property})"
    DELETE_RELATIONSHIP = "MATCH ()-[r { agent_type: $agent_type }]->() DELETE r"
    DELETE_NODE = "MATCH (n { agent_type: $agent_type }) DELETE n"

    MATCH_STATION_BY_DOMAIN_ID = "MATCH (n:Station) WHERE n.domain_id = $domain_id RETURN n"
    MATCH_ROUTE_BY_DOMAIN_ID = "MATCH (n:Route) WHERE n.domain_id = $domain_id RETURN n"
    MATCH_ROUTE_BY_STATION_IDS = "MATCH (s:Station)-[r:ROUTE_CONNECTION]->(n:Route) " \
                                 "WHERE s.domain_id in $station_ids " \
                                 "RETURN DISTINCT n, r ORDER BY r.raw_route_start_time LIMIT $limit"

    MATCH_TRANSITIONS_BY_ROUTE_ID = "MATCH (s1:Station)" \
                                    "-[r:TRANSITION { route_id: $route_id }]->" \
                                    "(s2: Station) RETURN DISTINCT " \
                                    "s1.station_id as departure_station_id, " \
                                    "r, s2.station_id as arrival_station_id " \
                                    "ORDER BY toInteger(r.transition_number)"

    MATCH_SHORTEST_PATHS = "MATCH (s1:Station), (s2:Station), " \
                           "n=allShortestPaths((s1)-[rs:TRANSITION*..{max_transitions}]->(s2)) " \
                           "WHERE s1.domain_id in $departure_station_ids " \
                           "AND s2.domain_id in $arrival_station_ids " \
                           "RETURN relationships(n) as transitions LIMIT $limit"

    def __init__(self, credentials, logger):
        self.driver = GraphDatabase.driver(
            'bolt://localhost',
            auth=basic_auth(credentials[0], credentials[1]))

        self.logger = logger
        self.create_indices()

        self.paths_sr_query_generator = MatchPathsWithSingleRouteQueryGenerator()
        self.paths_mr_query_generator = MatchPathsWithMultipleRoutesQueryGenerator()
        self.params_query_generator = MatchByParametersQueryGenerator()

        self.station_cache = {}
        self.routes_cache = {}

    @staticmethod
    def prepare_property(value):
        return value if not value is None else ''

    @staticmethod
    def prepare_properties(properties):
        prepared_properties = ('{key}: {value}'.format(
            key=item[0], value=repr(DbAccessor.prepare_property(item[1])))
            for item in properties.items())
        return ', '.join(prepared_properties)

    @staticmethod
    def set_properties(entity, properties):
        for item in properties.items():
            if not hasattr(entity, item[0]):
                entity.ensure_properties()[item[0]] = item[1]
            elif not isinstance(getattr(type(entity), item[0], None), property):
                setattr(entity, item[0], item[1])

    def execute(self, executor, default_value=None):
        result = default_value
        try:
            with self.driver.session() as session:
                with session.begin_transaction() as transaction:
                    result = executor(transaction)
        except (CypherError, DatabaseError) as e:
            self.logger.error(str(e))
        except Exception as e:
            self.logger.error(str(e))
        return result

    def create_indices(self):
        def indices_creator(transaction):
            indices = [
                ('Route', 'domain_id'),
                ('Route', 'route_number'),
                ('Station', 'domain_id'),
                ('Station', 'station_name_ua'),
                ('Station', 'station_name_ru'),
                ('Station', 'station_name_en'),
            ]
            for index in indices:
                transaction.run(self.CREATE_INDEX.format(label=index[0], property=index[1]))
        self.execute(indices_creator)

    def create_station(self, station, transaction):
        properties = {
            'domain_id': station.domain_id,
            'agent_type': station.agent_type,
            'station_id': station.station_id
        }

        if station.get_properties():
            properties.update(station.get_properties())

        station_query = self.CREATE_NODE.format(
            label='Station',
            properties=self.prepare_properties(properties))

        transaction.run(station_query)

    def create_route(self, route, transaction):
        properties = {
            'domain_id': route.domain_id,
            'agent_type': route.agent_type,
            'route_id': route.route_id,
            'route_number': route.route_number,
            'active_to_date': self.prepare_property(route.active_to_date),
            'active_from_date': self.prepare_property(route.active_from_date)
        }

        properties.update(route.get_properties())
        route_query = self.CREATE_NODE.format(
            label='Route',
            properties=self.prepare_properties(properties))
        transaction.run(route_query)

        departure_station_id = None
        departure_time = None
        transaction_number = 0

        for i, route_point in enumerate(route.route_points):
            raw_route_start_time = time_to_minutes(
                route_point.arrival_time
                if route_point.arrival_time
                else route_point.departure_time
            )

            properties = {
                'agent_type': route.agent_type,
                'station_number': i,
                'raw_route_start_time': raw_route_start_time
            }

            route_connection_query = self.CREATE_ROUTE_CONNECTION.format(
                route_domain_id=route.domain_id,
                station_domain_id=Station.get_domain_id(route.agent_type, route_point.station_id),
                properties=self.prepare_properties(properties))
            transaction.run(route_connection_query)

            if departure_time and departure_station_id:
                properties = {
                    'agent_type': route.agent_type,
                    'route_id': route.route_id,
                    'departure_time': departure_time,
                    'arrival_time': route_point.arrival_time,
                    'transition_number': transaction_number
                }

                transition_query = self.CREATE_TRANSITION.format(
                    from_domain_id=Station.get_domain_id(route.agent_type, departure_station_id),
                    to_domain_id=Station.get_domain_id(route.agent_type, route_point.station_id),
                    properties=self.prepare_properties(properties))
                transaction.run(transition_query)

                transaction_number += 1

            departure_station_id = route_point.station_id
            departure_time = route_point.departure_time

    def extract_station(self, data_item):
        properties = data_item['n'].properties

        station = self.station_cache.get(properties.get('domain_id'))
        if station:
            return station

        station = Station(properties['agent_type'], properties['station_id'])
        self.set_properties(station, properties)

        self.station_cache[station.domain_id] = station
        return station

    def extract_route(self, data_item, transaction, node_name='n'):
        properties = data_item[node_name].properties

        route = self.routes_cache.get(properties.get('domain_id'))
        if route:
            return route

        route = Route(properties['agent_type'], properties['route_id'])
        self.set_properties(route, properties)

        result = transaction.run(
            self.MATCH_TRANSITIONS_BY_ROUTE_ID,
            {'route_id': route.route_id}
        )

        data = result.data()
        if data:
            arrival_time = ''
            for i, data_item in enumerate(data):
                properties = data_item['r'].properties
                station_id = data_item['departure_station_id']

                route_point = RoutePoint(route.agent_type, route.route_id, station_id)
                route_point.arrival_time = arrival_time
                route_point.departure_time = properties['departure_time']
                route.add_route_point(route_point)

                arrival_time = properties['arrival_time']

                if len(data) - 1 == i:
                    station_id = data_item['arrival_station_id']

                    route_point = RoutePoint(route.agent_type, route.route_id, station_id)
                    route_point.arrival_time = arrival_time
                    route_point.departure_time = ''
                    route.add_route_point(route_point)

        self.routes_cache[route.domain_id] = route
        return route

    def __get_station(self, domain_id, transaction):
        result = transaction.run(
            self.MATCH_STATION_BY_DOMAIN_ID,
            {'domain_id': domain_id})
        if result:
            data = result.data()
            return self.extract_station(data[0]) if data else None
        return None

    def get_station(self, domain_id):
        station = self.station_cache.get(domain_id)
        if station:
            return station
        return self.execute(lambda transaction: self.__get_station(domain_id, transaction))

    def __get_route(self, domain_id, transaction):
        result = transaction.run(
            self.MATCH_ROUTE_BY_DOMAIN_ID,
            {'domain_id': domain_id})
        if result:
            data = result.data()
            return self.extract_route(data[0], transaction) if data else None
        return None

    def get_route(self, domain_id):
        route = self.routes_cache.get(domain_id)
        if route:
            return route
        return self.execute(lambda transaction: self.__get_route(domain_id, transaction))

    def find_stations(self, station_names, search_mode, limit):
        def stations_getter(transaction):
            stations = []

            stations_query = self.params_query_generator.generate_query(
                label='Station', search_mode=search_mode,
                property_names=['station_name_ua', 'station_name_en', 'station_name_ru'],
                property_values=station_names
            )

            if stations_query:
                result = transaction.run(stations_query, {'limit': limit})
                data = result.data()
                if data:
                    stations.extend(map(lambda data_item: self.extract_station(data_item), data))

                    for station in stations:
                        self.station_cache[station.domain_id] = station

            return stations

        return self.execute(stations_getter, [])

    def find_routes_by_route_numbers(self, route_numbers, search_mode, limit):
        def routes_getter(transaction):
            routes = []

            routes_query = self.params_query_generator.generate_query(
                label='Route', search_mode=search_mode,
                property_names=['route_number'],
                property_values=route_numbers
            )

            if routes_query:
                result = transaction.run(routes_query, {'limit': limit})
                data = result.data()
                if data:
                    routes.extend(
                        map(lambda data_item: self.extract_route(data_item, transaction), data)
                    )
            return routes

        return self.execute(routes_getter, [])

    def find_routes_by_station_ids(self, station_ids, limit):
        def routes_getter(transaction):
            routes = []

            result = transaction.run(
                self.MATCH_ROUTE_BY_STATION_IDS,
                {'station_ids': station_ids, 'limit': limit}
            )
            data = result.data()
            if data:
                routes.extend(
                    map(lambda data_item: self.extract_route(data_item, transaction), data)
                )
            return routes

        return self.execute(routes_getter, [])

    def find_paths_with_single_route(self, station_ids, limit):

        def routes_getter(transaction):
            paths = []

            stations_count = len(station_ids)
            paths_query = self.paths_sr_query_generator.generate_query(station_ids)

            parameters = {'limit': limit}
            for i, ids in enumerate(station_ids):
                parameters['station_ids_{}'.format(i + 1)] = ids

            result = transaction.run(paths_query, parameters)
            data = result.data()
            if data:
                for data_item in data:
                    path = Path()
                    node_name = 'n'
                    first_connection = data_item['r1']
                    second_connection = data_item['r{}'.format(stations_count)]

                    route = self.extract_route(data_item, transaction, node_name=node_name)
                    departure_route_point = first_connection.properties['station_number']
                    arrival_route_point = second_connection.properties['station_number']
                    path.add_path_item(PathItem(route, departure_route_point, arrival_route_point))
                    paths.append(path)
            return paths

        return self.execute(routes_getter, [])

    def find_paths_with_multiple_routes(self, station_ids, limit):

        def routes_getter(transaction):
            paths = []

            transfers_count = len(station_ids) - 2
            paths_query = self.paths_mr_query_generator.generate_query(station_ids)

            parameters = {'limit': limit}
            for i, ids in enumerate(station_ids):
                parameters['station_ids_{}'.format(i + 1)] = ids

            result = transaction.run(paths_query, parameters)
            data = result.data()
            if data:
                for data_item in data:
                    path = Path()
                    for i in range(transfers_count + 1):
                        node_name = 'n{}'.format(i + 1)
                        first_connection = data_item['r{}'.format(2 * i + 1)]
                        second_connection = data_item['r{}'.format(2 * i + 2)]

                        route = self.extract_route(data_item, transaction, node_name=node_name)
                        departure_route_point = first_connection.properties['station_number']
                        arrival_route_point = second_connection.properties['station_number']
                        path.add_path_item(PathItem(route, departure_route_point, arrival_route_point))
                    paths.append(path)
            return paths

        return self.execute(routes_getter, [])

    def find_shortest_paths(self, departure_station_ids, arrival_station_ids,
                            max_transitions, limit):
        def routes_getter(transaction):
            paths = []
            routes_cache = {}

            result = transaction.run(
                self.MATCH_SHORTEST_PATHS.format(max_transitions=max_transitions),
                {'departure_station_ids': departure_station_ids,
                 'arrival_station_ids': arrival_station_ids,
                 'limit': limit}
            )
            data = result.data()
            if data:
                for data_item in data:
                    path = Path()
                    transitions = data_item['transitions']
                    for transition in transitions:
                        agent_type = transition.properties['agent_type']
                        route_id = Route.get_domain_id(agent_type, transition.properties['route_id'])
                        transition_number = int(transition.properties['transition_number'])

                        if route_id in routes_cache:
                            route = routes_cache[route_id]
                        else:
                            route = self.__get_route(route_id, transaction)
                            routes_cache[route_id] = route

                        departure_route_point = transition_number
                        arrival_route_point = transition_number + 1
                        path.add_path_item(PathItem(route, departure_route_point, arrival_route_point))
                    paths.append(path)
            return paths

        return self.execute(routes_getter, [])

    def build_model(self, model):
        def model_builder(transaction):

            self.logger.debug('DbAccessor: building \'{}\' model'.format(model.agent_type))

            self.remove_model(model.agent_type, transaction)
            self.station_cache.clear()
            for station in model.stations.values():
                self.create_station(station, transaction)
            for route in model.routes.values():
                self.create_route(route, transaction)

            self.logger.debug('DbAccessor: built \'{}\' model'.format(model.agent_type))

        self.execute(model_builder)

    def remove_model(self, agent_type, transaction):
        transaction.run(self.DELETE_RELATIONSHIP, {'agent_type': agent_type})
        transaction.run(self.DELETE_NODE, {'agent_type': agent_type})
