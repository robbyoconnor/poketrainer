"""
pgoapi - Pokemon Go API
Copyright (c) 2016 tjado <https://github.com/tejado>
Modifications Copyright (c) 2016 j-e-k <https://github.com/j-e-k>
Modifications Copyright (c) 2016 Brad Smith <https://github.com/infinitewarp>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.

Author: tjado <https://github.com/tejado>
Modifications by: j-e-k <https://github.com/j-e-k>
Modifications by: Brad Smith <https://github.com/infinitewarp>
"""

from __future__ import absolute_import

import json
import logging
from collections import defaultdict
from itertools import chain
from time import time

import eventlet
import gevent
import six
from cachetools import TTLCache
from gevent.coros import BoundedSemaphore

from library import api
from pgoapi.pgoapi import PGoApi
# from library.api.pgoapi import protos
from library.api.pgoapi.protos.POGOProtos.Inventory import Item_pb2 as Item_Enums

from .inventory import Inventory as Player_Inventory
from .player_stats import PlayerStats as PlayerStats
from .poke_utils import (create_capture_probability, get_inventory_data,
                         get_item_name, get_pokemon_by_long_id)
from .pokedex import pokedex

from helper.exceptions import (AuthException, TooManyEmptyResponses)
from .location import (distance_in_meters, filtered_forts,
                       get_increments, get_location, get_neighbors, get_route)
from .player import Player as Player
from .pokemon import POKEMON_NAMES, Pokemon
from .release.base import ReleaseMethodFactory
from .config import Config

if six.PY3:
    from builtins import map as imap
elif six.PY2:
    from itertools import imap

logger = logging.getLogger(__name__)


class Poketrainer:
    def __init__(self, api_wrapper):

        self.counter = 0
        self.log = logging.getLogger(__name__)

        # objects
        self.api_wrapper = api_wrapper
        self.config = api_wrapper.config
        self.releaseMethodFactory = ReleaseMethodFactory(api_wrapper.config)
        self.player = Player({})
        self.player_stats = PlayerStats({})
        self.inventory = Player_Inventory(self.config.ball_priorities, [])

        self.api = api_wrapper.api
        self._origPosF = (0, 0, 0)
        self._posf = (0, 0, 0)

        # config values that might be changed during runtime
        self.step_size = self.config.step_size
        self.should_catch_pokemon = self.config.should_catch_pokemon

        # timers, counters and triggers
        self.pokemon_caught = 0
        self._last_got_map_objects = 0
        self._map_objects_rate_limit = 10.0
        self._error_counter = 0
        self._error_threshold = 10
        self._heartbeat_number = 5
        self._last_egg_use_time = 0
        self._farm_mode_triggered = False
        self.start_time = time()
        self.exp_start = None

        # caches
        self.encountered_pokemons = TTLCache(maxsize=120, ttl=self._map_objects_rate_limit * 2)
        self.visited_forts = TTLCache(maxsize=120, ttl=self.config.skip_visited_fort_duration)
        self.map_objects = {}

        # threading / locking
        self.sem = BoundedSemaphore(1)
        self.persist_lock = False

        # Sanity checking
        self.config.farm_items_enabled = self.config.farm_items_enabled and self.config.experimental and self.should_catch_pokemon  # Experimental, and we needn't do this if we're farming anyway
        if (
                                self.config.farm_items_enabled and
                                self.config.farm_ignore_pokeball_count and
                            self.config.farm_ignore_greatball_count and
                        self.config.farm_ignore_ultraball_count and
                    self.config.farm_ignore_masterball_count
        ):
            self.config.farm_items_enabled = False
            self.log.warn("FARM_ITEMS has been disabled due to all Pokeball counts being ignored.")
        elif self.config.farm_items_enabled and not (
                    self.config.pokeball_farm_threshold < self.config.pokeball_continue_threshold):
            self.config.farm_items_enabled = False
            self.log.warn(
                "FARM_ITEMS has been disabled due to farming threshold being below the continue. Set 'CATCH_POKEMON' to 'false' to enable captureless traveling.")

    def sleep(self, t):
        self.api_wrapper.sleep(t)

    def heartbeat(self):
        # making a standard call to update position, etc
        req = self.api.create_request()
        req.get_player()
        if self._heartbeat_number % 10 == 0:
            req.check_awarded_badges()
            req.get_inventory()
        res = req.call()
        if not res or res.get("direction", -1) == 102:
            self.log.error("There were a problem responses for api call: %s. Restarting!!!", res)
            raise AuthException("Token probably expired?")
        self.log.debug(
            'Heartbeat dictionary: \n\r{}'.format(json.dumps(res, indent=2, default=lambda obj: obj.decode('utf8'))))

        responses = res.get('responses', {})
        if 'GET_PLAYER' in responses:
            self.player = Player(responses.get('GET_PLAYER', {}).get('player_data', {}))
            self.log.info("Player Info: {0}, Pokemon Caught in this run: {1}".format(self.player, self.pokemon_caught))

        if 'GET_INVENTORY' in res.get('responses', {}):

            # update objects
            inventory_items = responses.get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
            self.inventory = Player_Inventory(self.config.ball_priorities, inventory_items)
            for inventory_item in self.inventory.inventory_items:
                if "player_stats" in inventory_item['inventory_item_data']:
                    self.player_stats = PlayerStats(
                        inventory_item['inventory_item_data']['player_stats'],
                        self.start_time, self.exp_start,
                        self.pokemon_caught
                    )
                    if self.exp_start is None:
                        self.exp_start = self.player_stats.run_exp_start
                    self.log.info("Player Stats: {}".format(self.player_stats))
            if self.config.list_inventory_before_cleanup:
                self.log.info("Player Items Before Cleanup: %s", self.inventory)
            self.log.debug(self.cleanup_inventory(self.inventory.inventory_items))
            self.log.info("Player Inventory after cleanup: %s", self.inventory)
            if self.config.list_pokemon_before_cleanup:
                self.log.info(get_inventory_data(res, self.player_stats.level, self.config.score_method,
                                                 self.config.score_settings))

            # save data dump
            with open("data_dumps/%s.json" % self.config.username, "w") as f:
                responses['lat'] = self._posf[0]
                responses['lng'] = self._posf[1]
                responses['hourly_exp'] = self.player_stats.run_hourly_exp
                f.write(json.dumps(responses, indent=2, default=lambda obj: obj.decode('utf8')))

            # maintenance
            self.incubate_eggs()
            self.use_lucky_egg()
            self.attempt_evolve(self.inventory.inventory_items)
            self.cleanup_pokemon(self.inventory.inventory_items)

            # Farm precon
            if self.config.farm_items_enabled:
                pokeball_count = 0
                if not self.config.farm_ignore_pokeball_count:
                    pokeball_count += self.inventory.poke_balls
                if not self.config.farm_ignore_greatball_count:
                    pokeball_count += self.inventory.great_balls
                if not self.config.farm_ignore_ultraball_count:
                    pokeball_count += self.inventory.ultra_balls
                if not self.config.farm_ignore_masterball_count:
                    pokeball_count += self.inventory.master_balls
                if self.config.pokeball_farm_threshold > pokeball_count and not self._farm_mode_triggered:
                    self.should_catch_pokemon = False
                    self._farm_mode_triggered = True
                    self.log.info("Player only has %s Pokeballs, farming for more...", pokeball_count)
                    if self.config.farm_override_step_size != -1:
                        self.step_size = self.config.farm_override_step_size
                        self.log.info("Player has changed speed to %s", self.step_size)
                elif self.config.pokeball_continue_threshold <= pokeball_count and self._farm_mode_triggered:
                    self.should_catch_pokemon = self.config.should_catch_pokemon  # Restore catch pokemon setting from config file
                    self._farm_mode_triggered = False
                    self.log.info("Player has %s Pokeballs, continuing to catch more!", pokeball_count)
                    if self.config.farm_override_step_size != -1:
                        self.step_size = self.config.step_size
                        self.log.info("Player has returned to normal speed of %s", self.step_size)
        self._heartbeat_number += 1
        return res

    def walk_to(self, loc, waypoints=[], directly=False):  # location in floats of course...
        # If we are going directly we don't want to follow a google maps
        # walkable route.
        use_google = self.config.use_google

        if directly is True:
            use_google = False

        step_size = self.step_size
        route_data = get_route(
            self._posf, loc, use_google, self.config.gmaps_api_key,
            self.config.experimental and self.config.spin_all_forts, waypoints,
            step_size=step_size
        )
        catch_attempt = 0
        base_travel_link = "https://www.google.com/maps/dir/%s,%s/" % (self._posf[0], self._posf[1])
        total_distance_traveled = 0
        total_distance = route_data['total_distance']
        self.log.info('===============================================')
        self.log.info("Total trip distance will be: {0:.2f} meters".format(total_distance))

        for step_data in route_data['steps']:
            step = (step_data['lat'], step_data['long'])
            step_increments = get_increments(self._posf, step, step_size)

            for i, next_point in enumerate(step_increments):
                distance_to_point = distance_in_meters(self._posf, next_point)
                total_distance_traveled += distance_to_point
                travel_link = '%s%s,%s' % (base_travel_link, next_point[0], next_point[1])
                self.log.info("Travel Link: %s", travel_link)
                self.api.set_position(*next_point)
                self.heartbeat()

                if directly is False:
                    if self.config.experimental and self.config.spin_all_forts:
                        self.spin_nearest_fort()

                # self.sleep(1)
                while self.catch_near_pokemon() and catch_attempt <= self.config.max_catch_attempts:
                    self.sleep(1)
                    catch_attempt += 1
                catch_attempt = 0

            self.log.info('Traveled %.2f meters of %.2f of the trip', total_distance_traveled, total_distance)
        self.log.info('===============================================')

    def walk_back_to_origin(self):
        self.walk_to(self._origPosF)

    def spin_nearest_fort(self):
        map_cells = self.nearby_map_objects().get('responses', {}).get('GET_MAP_OBJECTS', {}).get('map_cells', [])
        forts = self.flatmap(lambda c: c.get('forts', []), map_cells)
        destinations = filtered_forts(self._origPosF, self._posf, forts, self.config.stay_within_proximity,
                                      self.visited_forts)
        if destinations:
            nearest_fort = destinations[0][0]
            nearest_fort_dis = destinations[0][1]
            self.log.info("Nearest fort distance is {0:.2f} meters".format(nearest_fort_dis))

            # Fort is close enough to change our route and walk to
            if self.config.wander_steps > 0 and nearest_fort_dis > 40.00 and nearest_fort_dis <= self.config.wander_steps:
                self.walk_to_fort(destinations[0], directly=True)
            elif nearest_fort_dis <= 40.00:
                self.fort_search_pgoapi(nearest_fort, player_postion=self.api.get_position(),
                                        fort_distance=nearest_fort_dis)
                if 'lure_info' in nearest_fort and self.should_catch_pokemon:
                    self.disk_encounter_pokemon(nearest_fort['lure_info'])

        else:
            self.log.info('No spinnable forts within proximity. Or server returned no map objects.')
            self._error_counter += 1

    def spin_all_forts_visible(self):
        res = self.nearby_map_objects()
        map_cells = res.get('responses', {}).get('GET_MAP_OBJECTS', {}).get('map_cells', [])
        forts = self.flatmap(lambda c: c.get('forts', []), map_cells)
        destinations = filtered_forts(self._origPosF, self._posf, forts, self.config.stay_within_proximity,
                                      self.visited_forts)
        if not destinations:
            self.log.debug("No fort to walk to! %s", res)
            self.log.info('No more spinnable forts within proximity. Or server error')
            self._error_counter += 1
            self.walk_back_to_origin()
            return False
        if len(destinations) >= 20:
            destinations = destinations[:20]
        furthest_fort = destinations[0][0]
        self.log.info("Walking to fort at  http://maps.google.com/maps?q=%s,%s", furthest_fort['latitude'],
                      furthest_fort['longitude'])
        self.walk_to((furthest_fort['latitude'], furthest_fort['longitude']),
                     map(lambda x: "via:%f,%f" % (x[0]['latitude'], x[0]['longitude']), destinations[1:]))
        return True

    def return_to_start(self):
        self.api.set_position(*self._origPosF)

    def walk_to_fort(self, fort_data, directly=False):
        fort = fort_data[0]
        self.log.info(
            "Walking to fort at  http://maps.google.com/maps?q=%s,%s",
            fort['latitude'], fort['longitude'])
        self.walk_to((fort['latitude'], fort['longitude']), directly=directly)
        self.fort_search_pgoapi(fort, self.api.get_position(), fort_data[1])
        if 'lure_info' in fort and self.should_catch_pokemon:
            self.disk_encounter_pokemon(fort['lure_info'])

    def spin_near_fort(self):
        res = self.nearby_map_objects()
        map_cells = res.get('responses', {}).get('GET_MAP_OBJECTS', {}).get('map_cells', [])
        forts = self.flatmap(lambda c: c.get('forts', []), map_cells)
        destinations = filtered_forts(self._origPosF, self._posf, forts, self.config.stay_within_proximity,
                                      self.visited_forts)
        if not destinations:
            self.log.debug("No fort to walk to! %s", res)
            self.log.info('No more spinnable forts within proximity. Returning back to origin')
            self.walk_back_to_origin()
            return False

        for fort_data in destinations:
            self.walk_to_fort(fort_data)

        return True

    def catch_near_pokemon(self):
        if self.should_catch_pokemon is False:
            return False

        map_cells = self.nearby_map_objects().get('responses', {}).get('GET_MAP_OBJECTS', {}).get('map_cells', [])
        pokemons = self.flatmap(lambda c: c.get('catchable_pokemons', []), map_cells)
        pokemons = filter(lambda p: (p['encounter_id'] not in self.encountered_pokemons), pokemons)

        # catch first pokemon:
        origin = (self._posf[0], self._posf[1])
        pokemon_distances = [(pokemon, distance_in_meters(origin, (pokemon['latitude'], pokemon['longitude']))) for
                             pokemon
                             in pokemons]
        if pokemons:
            self.log.debug("Nearby pokemon: : %s", pokemon_distances)
            self.log.info("Nearby Pokemon: %s",
                          ", ".join(map(lambda x: POKEMON_NAMES[str(x['pokemon_id'])], pokemons)))
        else:
            self.log.info("No nearby pokemon")
        catches_successful = False
        for pokemon_distance in pokemon_distances:
            target = pokemon_distance
            self.log.debug("Catching pokemon: : %s, distance: %f meters", target[0], target[1])
            catches_successful &= self.encounter_pokemon(target[0])
            # self.sleep(random.randrange(4, 8))
        return catches_successful

    def cleanup_inventory(self, inventory_items=None):
        if not inventory_items:
            self.sleep(0.2)
            inventory_items = self.api.get_inventory() \
                .get('responses', {}).get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
        item_count = 0
        for inventory_item in inventory_items:
            if "item" in inventory_item['inventory_item_data']:
                item = inventory_item['inventory_item_data']['item']
                if (
                                    item['item_id'] in self.config.min_items and
                                    "count" in item and
                                item['count'] > self.config.min_items[item['item_id']]
                ):
                    recycle_count = item['count'] - self.config.min_items[item['item_id']]
                    item_count += item['count'] - recycle_count
                    self.log.info("Recycling {0} {1}(s)".format(recycle_count, get_item_name(item['item_id'])))
                    self.sleep(0.2)
                    res = self.api.recycle_inventory_item(item_id=item['item_id'], count=recycle_count) \
                        .get('responses', {}).get('RECYCLE_INVENTORY_ITEM', {})
                    response_code = res.get('result', -1)
                    if response_code == 1:
                        self.log.info("{0}(s) recycled successfully. New count: {1}".format(get_item_name(
                            item['item_id']), res.get('new_count', 0)))
                    else:
                        self.log.info("Failed to recycle {0}, Code: {1}".format(get_item_name(item['item_id']),
                                                                                response_code))
                    self.sleep(1)
                elif "count" in item:
                    item_count += item['count']
        if item_count > 0:
            self.log.info("Inventory has {0}/{1} items".format(item_count, self.player.max_item_storage))
        return self.update_player_inventory()

    def get_caught_pokemons(self, inventory_items=None, as_json=False):
        if not inventory_items:
            self.sleep(0.2)
            inventory_items = self.api.get_inventory() \
                .get('responses', {}).get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
        caught_pokemon = defaultdict(list)
        for inventory_item in inventory_items:
            if "pokemon_data" in inventory_item['inventory_item_data'] and not inventory_item['inventory_item_data'][
                'pokemon_data'].get("is_egg", False):
                # is a pokemon:
                pokemon_data = inventory_item['inventory_item_data']['pokemon_data']
                pokemon = Pokemon(pokemon_data, self.player_stats.level, self.config.score_method,
                                  self.config.score_settings)

                if not pokemon.is_egg:
                    caught_pokemon[pokemon.pokemon_id].append(pokemon)
        if as_json:
            return json.dumps(caught_pokemon, default=lambda p: p.__dict__)  # reduce the data sent?
        return caught_pokemon

    def get_player_info(self, as_json=True):
        return self.player.to_json()

    def do_release_pokemon_by_id(self, p_id):
        release_res = self.api.release_pokemon(pokemon_id=int(p_id)).get('responses', {}).get('RELEASE_POKEMON', {})
        status = release_res.get('result', -1)
        return status

    def do_release_pokemon(self, pokemon):
        self.log.info("Releasing pokemon: %s", pokemon)
        if self.do_release_pokemon_by_id(pokemon.id):
            self.log.info("Successfully Released Pokemon %s", pokemon)
        else:
            # self.log.debug("Failed to release pokemon %s, %s", pokemon, release_res)  # FIXME release_res is not in scope!
            self.log.info("Failed to release Pokemon %s", pokemon)
        self.sleep(1.0)

    def get_pokemon_stats(self, inventory_items=None):
        if not inventory_items:
            inventory_items = self.api.get_inventory() \
                .get('responses', {}).get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
        caught_pokemon = self.get_caught_pokemons(inventory_items)
        for pokemons in caught_pokemon.values():
            for pokemon in pokemons:
                self.log.info("%s", pokemon)

    def cleanup_pokemon(self, inventory_items=None):
        if not inventory_items:
            inventory_items = self.api.get_inventory() \
                .get('responses', {}).get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
        caught_pokemon = self.get_caught_pokemons(inventory_items)
        releaseMethod = self.releaseMethodFactory.getReleaseMethod()
        for pokemonId, pokemons in caught_pokemon.iteritems():
            pokemonsToRelease, pokemonsToKeep = releaseMethod.getPokemonToRelease(pokemonId, pokemons)

            if self.config.pokemon_cleanup_testing_mode:
                for pokemon in pokemonsToRelease:
                    self.log.info("(TESTING) Would release pokemon: %s", pokemon)
                for pokemon in pokemonsToKeep:
                    self.log.info("(TESTING) Would keep pokemon: %s", pokemon)
            else:
                for pokemon in pokemonsToRelease:
                    self.do_release_pokemon(pokemon)

    def attempt_evolve(self, inventory_items=None):
        if not inventory_items:
            self.sleep(0.2)
            inventory_items = self.api.get_inventory() \
                .get('responses', {}).get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
        caught_pokemon = self.get_caught_pokemons(inventory_items)
        self.inventory = Player_Inventory(self.config.ball_priorities, inventory_items)
        for pokemons in caught_pokemon.values():
            if len(pokemons) > self.config.min_similar_pokemon:
                pokemons = sorted(pokemons, key=lambda x: (x.cp, x.iv), reverse=True)
                for pokemon in pokemons[self.config.min_similar_pokemon:]:
                    # If we can't evolve this type of pokemon anymore, don't check others.
                    if not self.attempt_evolve_pokemon(pokemon):
                        break

    def attempt_evolve_pokemon(self, pokemon):
        if self.is_pokemon_eligible_for_evolution(pokemon=pokemon):
            self.log.info("Evolving pokemon: %s", pokemon)
            self.sleep(0.2)
            evo_res = self.api.evolve_pokemon(pokemon_id=pokemon.id).get('responses', {}).get('EVOLVE_POKEMON', {})
            status = evo_res.get('result', -1)
            # self.sleep(3)
            if status == 1:
                evolved_pokemon = Pokemon(evo_res.get('evolved_pokemon_data', {}),
                                          self.player_stats.level, self.config.score_method, self.config.score_settings)
                # I don' think we need additional stats for evolved pokemon. Since we do not do anything with it.
                # evolved_pokemon.pokemon_additional_data = self.game_master.get(pokemon.pokemon_id, PokemonData())
                self.log.info("Evolved to %s", evolved_pokemon)
                self.update_player_inventory()
                return True
            else:
                self.log.debug("Could not evolve Pokemon %s", evo_res)
                self.log.info("Could not evolve pokemon %s | Status %s", pokemon, status)
                self.update_player_inventory()
                return False
        else:
            return False

    def is_pokemon_eligible_for_evolution(self, pokemon):
        candy_have = self.inventory.pokemon_candy.get(
            self.config.pokemon_evolution_family.get(pokemon.pokemon_id, None), -1)
        candy_needed = self.config.pokemon_evolution.get(pokemon.pokemon_id, None)
        return candy_have > candy_needed and \
               pokemon.pokemon_id not in self.config.keep_pokemon_ids \
               and not pokemon.is_favorite \
               and pokemon.pokemon_id in self.config.pokemon_evolution

    def disk_encounter_pokemon(self, lureinfo, retry=False):
        try:
            self.update_player_inventory()
            if not self.inventory.can_attempt_catch():
                self.log.info("No balls to catch %s, exiting disk encounter", self.inventory)
                return False
            encounter_id = lureinfo['encounter_id']
            fort_id = lureinfo['fort_id']
            position = self._posf
            self.log.debug("At Fort with lure %s".encode('utf-8', 'ignore'), lureinfo)
            self.log.info("At Fort with Lure AND Active Pokemon %s",
                          POKEMON_NAMES.get(str(lureinfo.get('active_pokemon_id', 0)), "NA"))
            resp = self.api.disk_encounter(encounter_id=encounter_id, fort_id=fort_id, player_latitude=position[0],
                                           player_longitude=position[1]) \
                .get('responses', {}).get('DISK_ENCOUNTER', {})
            result = resp.get('result', -1)
            if result == 1 and 'pokemon_data' in resp and 'capture_probability' in resp:
                pokemon = Pokemon(resp.get('pokemon_data', {}))
                capture_probability = create_capture_probability(resp.get('capture_probability', {}))
                self.log.debug("Attempt Encounter: %s", json.dumps(resp, indent=4, sort_keys=True))
                return self.do_catch_pokemon(encounter_id, fort_id, capture_probability, pokemon)
            elif result == 5:
                self.log.info("Couldn't catch %s Your pokemon bag was full, attempting to clear and re-try",
                              POKEMON_NAMES.get(str(lureinfo.get('active_pokemon_id', 0)), "NA"))
                self.cleanup_pokemon()
                if not retry:
                    return self.disk_encounter_pokemon(lureinfo, retry=True)
            elif result == 2:
                self.log.info("Could not start Disk (lure) encounter for pokemon: %s, not available",
                              POKEMON_NAMES.get(str(lureinfo.get('active_pokemon_id', 0)), "NA"))
            else:
                self.log.info("Could not start Disk (lure) encounter for pokemon: %s, Result: %s",
                              POKEMON_NAMES.get(str(lureinfo.get('active_pokemon_id', 0)), "NA"),
                              result)
        except Exception as e:
            self.log.error("Error in disk encounter %s", e)
            return False

    def do_catch_pokemon(self, encounter_id, spawn_point_id, capture_probability, pokemon):
        self.log.info("Catching Pokemon: %s", pokemon)
        catch_attempt = self.attempt_catch(encounter_id, spawn_point_id, capture_probability)
        capture_status = catch_attempt.get('status', -1)
        if capture_status == 1:
            self.log.debug("Caught Pokemon: : %s", catch_attempt)
            self.log.info("Caught Pokemon:  %s", pokemon)
            self.pokemon_caught += 1
            return True
        elif capture_status == 3:
            self.log.debug("Pokemon fleed : %s", catch_attempt)
            self.log.info("Pokemon fleed:  %s", pokemon)
            return False
        elif capture_status == 2:
            self.log.debug("Pokemon escaped: : %s", catch_attempt)
            self.log.info("Pokemon escaped:  %s", pokemon)
            return False
        elif capture_status == 4:
            self.log.debug("Catch Missed: : %s", catch_attempt)
            self.log.info("Catch Missed:  %s", pokemon)
            return False
        else:
            self.log.debug("Could not catch pokemon: %s", catch_attempt)
            self.log.info("Could not catch pokemon:  %s", pokemon)
            self.log.info("Could not catch pokemon:  %s, status: %s", pokemon, capture_status)
            return False

    def encounter_pokemon(self, pokemon_data, retry=False,
                          new_loc=None):  # take in a MapPokemon from MapCell.catchable_pokemons
        # Update Inventory to make sure we can catch this mon
        try:
            self.update_player_inventory()
            if not self.inventory.can_attempt_catch():
                self.log.info("No balls to catch %s, exiting encounter", self.inventory)
                return False
            encounter_id = pokemon_data['encounter_id']
            spawn_point_id = pokemon_data['spawn_point_id']
            # begin encounter_id
            position = self.api.get_position()
            pokemon = Pokemon(pokemon_data)
            self.log.info("Trying initiate catching Pokemon: %s", pokemon)
            encounter = self.api.encounter(encounter_id=encounter_id,
                                           spawn_point_id=spawn_point_id,
                                           player_latitude=position[0],
                                           player_longitude=position[1]) \
                .get('responses', {}).get('ENCOUNTER', {})
            self.log.debug("Attempting to Start Encounter: %s", encounter)
            result = encounter.get('status', -1)
            if result == 1 and 'wild_pokemon' in encounter and 'capture_probability' in encounter:
                pokemon = Pokemon(encounter.get('wild_pokemon', {}).get('pokemon_data', {}))
                capture_probability = create_capture_probability(encounter.get('capture_probability', {}))
                self.log.debug("Attempt Encounter Capture Probability: %s",
                               json.dumps(encounter, indent=4, sort_keys=True))

                if new_loc:
                    # change loc for sniping
                    self.log.info("Teleporting to %f, %f before catching", new_loc[0], new_loc[1])
                    self.api.set_position(new_loc[0], new_loc[1], 0.0)
                    self.send_update_pos()
                    # self.sleep(2)

                self.encountered_pokemons[encounter_id] = pokemon_data
                return self.do_catch_pokemon(encounter_id, spawn_point_id, capture_probability, pokemon)
            elif result == 7:
                self.log.info("Couldn't catch %s Your pokemon bag was full, attempting to clear and re-try",
                              pokemon.pokemon_type)
                self.cleanup_pokemon()
                if not retry:
                    return self.encounter_pokemon(pokemon_data, retry=True, new_loc=new_loc)
            else:
                self.log.info("Could not start encounter for pokemon: %s, status %s", pokemon.pokemon_type, result)
            return False
        except Exception as e:
            self.log.error("Error in pokemon encounter %s", e)
            return False

    def incubate_eggs(self):
        if not self.config.egg_incubation_enabled:
            return
        if self.player_stats.km_walked > 0:
            for incubator in self.inventory.incubators_busy:
                incubator_start_km_walked = incubator.get('start_km_walked', self.player_stats.km_walked)

                incubator_egg_distance = incubator['target_km_walked'] - incubator_start_km_walked
                incubator_distance_done = self.player_stats.km_walked - incubator_start_km_walked
                if incubator_distance_done > incubator_egg_distance:
                    self.attempt_finish_incubation()
                    break
            for incubator in self.inventory.incubators_busy:
                incubator_start_km_walked = incubator.get('start_km_walked', self.player_stats.km_walked)

                incubator_egg_distance = incubator['target_km_walked'] - incubator_start_km_walked
                incubator_distance_done = self.player_stats.km_walked - incubator_start_km_walked
                self.log.info('Incubating %skm egg, %skm done', incubator_egg_distance,
                              round(incubator_distance_done, 2))
        for incubator in self.inventory.incubators_available:
            if incubator['item_id'] == 901:  # unlimited use
                pass
            elif self.config.use_disposable_incubators and incubator['item_id'] == 902:  # limited use
                pass
            else:
                continue
            eggs_available = self.inventory.eggs_available
            eggs_available = sorted(eggs_available, key=lambda egg: egg['creation_time_ms'],
                                    reverse=False)  # oldest first
            eggs_available = sorted(eggs_available, key=lambda egg: egg['egg_km_walked_target'],
                                    reverse=self.config.incubate_big_eggs_first)  # now sort as defined
            if not len(eggs_available) > 0 or not self.attempt_start_incubation(eggs_available[0], incubator):
                break

    def attempt_start_incubation(self, egg, incubator):
        self.log.info("Start incubating %skm egg", egg['egg_km_walked_target'])
        incubate_res = self.api.use_item_egg_incubator(item_id=incubator['id'], pokemon_id=egg['id']) \
            .get('responses', {}).get('USE_ITEM_EGG_INCUBATOR', {})
        status = incubate_res.get('result', -1)
        # self.sleep(3)
        if status == 1:
            self.log.info("Incubation started with %skm egg !", egg['egg_km_walked_target'])
            self.update_player_inventory()
            return True
        else:
            self.log.debug("Could not start incubating %s", incubate_res)
            self.log.info("Could not start incubating %s egg | Status %s", egg['egg_km_walked_target'], status)
            self.update_player_inventory()
            return False

    def attempt_finish_incubation(self):
        self.log.info("Checking for hatched eggs")
        self.sleep(0.2)
        hatch_res = self.api.get_hatched_eggs().get('responses', {}).get('GET_HATCHED_EGGS', {})
        status = hatch_res.get('success', -1)
        # self.sleep(3)
        if status == 1:
            self.update_player_inventory()
            for i, pokemon_id in enumerate(hatch_res['pokemon_id']):
                pokemon = get_pokemon_by_long_id(pokemon_id, self.inventory.inventory_items)
                self.log.info("Egg Hatched! XP +%s, Candy +%s, Stardust +%s, %s",
                              hatch_res['experience_awarded'][i],
                              hatch_res['candy_awarded'][i],
                              hatch_res['stardust_awarded'][i],
                              pokemon)
            return True
        else:
            self.log.debug("Could not get hatched eggs %s", hatch_res)
            self.log.info("Could not get hatched eggs Status %s", status)
            self.update_player_inventory()
            return False

    def main_loop(self):
        i = 0
        while True:
            if i > 300:
                return
            self.counter += 1
            self.log.info('Running Poketrainer %s - %s, %s', self.counter, self.api_wrapper.counter, self.api_wrapper.__name__)
            self.sleep(1.0)
            i += 1
        catch_attempt = 0
        self.heartbeat()
        while True:
            self.heartbeat()
            # self.sleep(1)

            if self.config.experimental and self.config.spin_all_forts:
                self.spin_all_forts_visible()
            else:
                self.spin_near_fort()
            # if catching fails 10 times, maybe you are sofbanned.
            # We can't actually use this as a basis for being softbanned. Pokemon Flee if you are softbanned (~stolencatkarma)
            while self.catch_near_pokemon() and catch_attempt <= self.config.max_catch_attempts:
                # self.sleep(4)
                catch_attempt += 1
                pass
            if catch_attempt > self.config.max_catch_attempts:
                self.log.warn("You have reached the maximum amount of catch attempts. Giving up after %s times",
                              catch_attempt)
            catch_attempt = 0

            if self._error_counter >= self._error_threshold:
                raise TooManyEmptyResponses('Too many errors in this run!!!')

    @staticmethod
    def flatmap(f, items):
        return list(chain.from_iterable(imap(f, items)))
