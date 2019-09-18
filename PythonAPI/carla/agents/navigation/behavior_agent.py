#!/usr/bin/env python

# Copyright (c) 2018 Intel Labs.
# authors: German Ros (german.ros@intel.com)
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

""" This module implements an agent that roams around a track following random
waypoints and avoiding other vehicles.
The agent also responds to traffic lights. """

import random
import numpy as np
import carla
from agents.navigation.agent import Agent
from agents.navigation.local_planner import LocalPlanner
from agents.navigation.global_route_planner import GlobalRoutePlanner
from agents.navigation.global_route_planner_dao import GlobalRoutePlannerDAO
from agents.navigation.local_planner import RoadOption

from agents.tools.misc import get_speed, positive

class BehaviorAgent(Agent):
    """
    BehaviorAgent implements an agent that navigates scenes to reach a given
    target destination, by computing the shortest possible path to it.
    This agent can correctly follow traffic signs, speed limitations,
    traffic lights, while also taking into account nearby vehicles. Lane changing
    decisions can be taken by analyzing the surrounding environment,
    such as overtaking or tailgating avoidance. Adding to these are possible
    behaviors, the agent can also keep safety distance from a car in front of it
    by tracking the instantaneous time to collision and keeping it in a certain range.
    Finally, different sets of behaviors are encoded in the agent, from cautious
    to a more aggressive ones.
    """

    def __init__(self, vehicle, ignore_traffic_light=False, behavior='Normal'):
        """
        :param vehicle: actor to apply to local planner logic onto
        """

        super(BehaviorAgent, self).__init__(vehicle)
        self.ignore_traffic_light = ignore_traffic_light
        self._local_planner = LocalPlanner(
            self, self._vehicle)
        self._hop_resolution = 3.0
        self._path_seperation_hop = 2
        self._path_seperation_threshold = 0.5
        self._grp = None
        self.look_ahead_steps = 0

        # Vehicle information
        self.speed = 0
        self.speed_limit = 0
        self.direction = None
        self.incoming_direction = None
        self.incoming_waypoint = None
        self.start_waypoint = None
        self.end_waypoint = None
        self.is_at_traffic_light = 0
        self.light_state = "Green"
        self.light_id_to_ignore = 0

        # Parameters for agent behavior
        if behavior == 'cautious':
            self.max_speed = 40
            self.speed_increase_perc = 10
            self.speed_lim_dist = 12
            self.speed_decrease = 12
            self.safety_time = 3
            self.min_proximity_threshold = 12
            self.braking_distance = 7
            self.overtake_counter = -1
            self.tailgate_counter = 0

        elif behavior == 'normal':
            self.max_speed = 60
            self.speed_increase_perc = 15
            self.speed_lim_dist = 6
            self.speed_decrease = 10
            self.safety_time = 3
            self.min_proximity_threshold = 10
            self.braking_distance = 6
            self.overtake_counter = 0
            self.tailgate_counter = 0

        elif behavior == 'aggressive':
            self.max_speed = 70
            self.speed_increase_perc = 20
            self.speed_lim_dist = 3
            self.speed_decrease = 8
            self.safety_time = 3
            self.min_proximity_threshold = 8
            self.braking_distance = 5
            self.overtake_counter = 0
            self.tailgate_counter = -1

    def update_information(self, world):
        """
        This method updates the information regarding the ego
        vehicle based on the surrounding world.
        """
        self.speed = get_speed(self._vehicle)
        self.speed_limit = world.player.get_speed_limit()
        self._local_planner.set_speed(self.speed_limit)
        self.direction = self._local_planner._target_road_option
        if self.direction is None:
            self.direction = RoadOption.LANEFOLLOW

        self.look_ahead_steps = int((self.speed)/10)

        self.incoming_waypoint, self.incoming_direction = \
            self._local_planner.get_incoming_waypoint_and_direction(steps=self.look_ahead_steps)
        if self.incoming_direction is None:
            self.incoming_direction = RoadOption.LANEFOLLOW

        self.is_at_traffic_light = world.player.is_at_traffic_light()
        if self.ignore_traffic_light:
            self.light_state = "Green"
        else:
            # This method also includes stop signs and intersections.
            self.light_state = str(self._vehicle.get_traffic_light_state())

    def set_destination(self, end_location, start_location, clean=False):
        """
        This method creates a list of waypoints from agent's position to destination location
        based on the route returned by the global router.
        """
        if clean:
            self._local_planner._waypoints_queue.clear()
        self.start_waypoint = self._map.get_waypoint(start_location)
        self.end_waypoint = self._map.get_waypoint(end_location)

        route_trace = self._trace_route(self.start_waypoint, self.end_waypoint)
        assert route_trace

        self._local_planner.set_global_plan(route_trace)

    def reroute(self, spawn_points):
        """
        This method implements re-routing for vehicles approaching its destination.
        It finds a new target and computes another path to reach it.
        :param spawn_points: list of possible destinations for the agent
        """

        print("Target almost reached, setting new destination...")
        random.shuffle(spawn_points)
        new_start = self._local_planner._waypoints_queue[-1][0].transform.location
        destination = spawn_points[0].location if spawn_points[0].location != new_start else spawn_points[1].location
        print("New destination: " + str(destination))

        self.set_destination(destination, new_start)

    def _trace_route(self, start_waypoint, end_waypoint):
        """
        This method sets up a global router and returns the
        optimal route from start_waypoint to end_waypoint
        :param start_waypoint: initial position
        :param end_waypoint: final position
        """
        # Setting up global router
        if self._grp is None:
            dao = GlobalRoutePlannerDAO(
                self._vehicle.get_world().get_map(), sampling_resolution=self._hop_resolution,
                world=self._vehicle.get_world())
            grp = GlobalRoutePlanner(dao)
            grp.setup()
            self._grp = grp

        # Obtain route plan
        route = self._grp.trace_route(
            start_waypoint.transform.location,
            end_waypoint.transform.location)

        return route

    def traffic_light_manager(self, waypoint):
        """
        This method is in charge of behaviors for red lights and stops.

        WARNING: What follows is a proxy to avoid having a car brake after running a yellow light.
        This happens because the car is still under the influence of the semaphore,
        even after passing it. So, the semaphore id is temporarely saved to
        ignore it and go around this issue, until the car is near a new one.
        """

        light_id = self._vehicle.get_traffic_light().id if self._vehicle.get_traffic_light() is not None else 0

        if self.light_state == "Red":
            if not waypoint.is_junction and \
            (self.light_id_to_ignore != light_id or light_id == 0):
                return 1
            elif waypoint.is_junction and light_id != 0:
                self.light_id_to_ignore = light_id
        if self.light_id_to_ignore != light_id:
            self.light_id_to_ignore = 0
        return 0

    def _overtake(self, waypoint, vehicle_list):
        """
        This method is in charge of overtaking behaviors.
        :param waypoint: current waypoint of the agent
        :param vehicle_list: list of all the nearby vehicles
        """

        if (waypoint.lane_change == carla.LaneChange.Left or \
            waypoint.lane_change == carla.LaneChange.Both) and \
            waypoint.lane_id*waypoint.get_left_lane().lane_id > 0: # Checks for same road.
            new_vehicle_state, _, _ = self._is_vehicle_on_left_lane_hazard(vehicle_list, \
                max(self.min_proximity_threshold, self.speed_limit/2))
            if not new_vehicle_state:
                print("Overtaking to the left!")
                self.overtake_counter = 200
                self.set_destination(self.end_waypoint.transform.location, \
                    waypoint.get_left_lane().transform.location, clean=True)
        elif waypoint.lane_change == carla.LaneChange.Right and \
            waypoint.lane_id*waypoint.get_right_lane().lane_id > 0:
            new_vehicle_state, _, _ = self._is_vehicle_on_right_lane_hazard(vehicle_list, \
                max(self.min_proximity_threshold, self.speed_limit/2))
            if not new_vehicle_state:
                print("Overtaking to the right!")
                self.overtake_counter = 200
                self.set_destination(self.end_waypoint.transform.location, \
                        waypoint.get_right_lane().transform.location, clean=True)

    def _tailgating(self, waypoint, vehicle_list):
        """
        This method is in charge of tailgating behaviors.
        :param waypoint: current waypoint of the agent
        :param vehicle_list: list of all the nearby vehicles
        """

        behind_vehicle_state, behind_vehicle, _ = self._is_vehicle_behind_hazard(vehicle_list, \
            max(self.min_proximity_threshold, self.speed_limit/3))
        if behind_vehicle_state and self.speed < get_speed(behind_vehicle):
            if waypoint.lane_change == carla.LaneChange.Right or \
                waypoint.lane_change == carla.LaneChange.Both and \
                waypoint.lane_id*waypoint.get_right_lane().lane_id > 0:
                new_vehicle_state, _, _ = self._is_vehicle_on_right_lane_hazard(vehicle_list, \
                    max(self.min_proximity_threshold, self.speed_limit/2))
                if not new_vehicle_state:
                    print("Tailgating, moving to the right!")
                    self.tailgate_counter = 200
                    self.set_destination(self.end_waypoint.transform.location, \
                        waypoint.get_right_lane().transform.location, clean=True)
            elif waypoint.lane_change == carla.LaneChange.Left and \
                waypoint.lane_id*waypoint.get_left_lane().lane_id > 0:
                new_vehicle_state, _, _ = self._is_vehicle_on_left_lane_hazard(vehicle_list, \
                    max(self.min_proximity_threshold, self.speed_limit/2))
                if not new_vehicle_state:
                    print("Tailgating, moving to the left!")
                    self.tailgate_counter = 200
                    self.set_destination(self.end_waypoint.transform.location, \
                        waypoint.get_left_lane().transform.location, clean=True)

    def collision_and_car_avoid_manager(self, waypoint):
        """
        This module is in charge of warning in case of a collision
        and managing possible overtaking or tailgating chances.
        :param waypoint: current waypoint of the agent
        :return vehicle_state: True if there is a vehicle nearby, False if not
        :return vehicle: nearby vehicle
        :return distance: distance to nearby vehicle
        """

        vehicle_list = self._world.get_actors().filter("*vehicle*")
        dist = lambda v: v.get_location().distance(waypoint.transform.location)
        vehicle_list = [v for v in vehicle_list if dist(v) < 50]
        if self.direction == RoadOption.CHANGELANELEFT:
            vehicle_state, vehicle, distance = self._is_vehicle_on_left_lane_hazard(vehicle_list, \
            max(self.min_proximity_threshold, self.speed_limit/2))
        elif self.direction == RoadOption.CHANGELANERIGHT:
            vehicle_state, vehicle, distance = self._is_vehicle_on_right_lane_hazard(vehicle_list, \
            max(self.min_proximity_threshold, self.speed_limit/2))
        else:
            vehicle_state, vehicle, distance = self._is_vehicle_hazard(vehicle_list, \
            max(self.min_proximity_threshold, self.speed_limit/3))

            # Check for overtaking

            if vehicle_state and self.direction == RoadOption.LANEFOLLOW and \
                not waypoint.is_junction and self.speed > 10 and \
                self.overtake_counter == 0 and self.speed > get_speed(vehicle):
                self._overtake(waypoint, vehicle_list)

            # Check for tailgating

            elif not vehicle_state and self.direction == RoadOption.LANEFOLLOW and \
                not waypoint.is_junction and self.speed > 10 and \
                self.tailgate_counter == 0:
                self._tailgating(waypoint, vehicle_list)

        return vehicle_state, vehicle, distance

    def car_following_manager(self, vehicle, distance, debug=False):
        """
        Module in charge of car-following behaviors when there's
        someone in front of us.
        :param vehicle: car to follow
        :param distance: distance from vehicle
        :return control: carla.VehicleControl
        """
        vehicle_speed = get_speed(vehicle)
        delta_v = max(1, (self.speed - vehicle_speed)/3.6)
        ttc = distance/delta_v if delta_v != 0 else distance/np.nextafter(0., 1.)

        # Under safety time distance, slow down.
        if self.safety_time > ttc > 0.0:
            control = self._local_planner.run_step(
                target_speed=min(positive(vehicle_speed-self.speed_decrease), \
                min(self.max_speed, self.speed_limit-self.speed_lim_dist)), debug=debug)
        # Actual safety distance area, try to follow the speed of the vehicle in front.
        elif 2*self.safety_time > ttc >= self.safety_time:
            control = self._local_planner.run_step(
                target_speed=min(max(5, vehicle_speed-self.speed_decrease/2), \
                min(self.max_speed, self.speed_limit-self.speed_lim_dist)), debug=debug)
        # Normal behavior.
        else:
            control = self._local_planner.run_step(
                target_speed=min(self.speed+(self.speed_increase_perc*self.speed)/100, \
                min(self.max_speed, self.speed_limit-self.speed_lim_dist)), debug=debug)
        return control

    def run_step(self, debug=False):
        """
        Execute one step of navigation.
        :return control: carla.VehicleControl
        """
        control = None
        if self.tailgate_counter > 0:
            self.tailgate_counter -= 1
        if self.overtake_counter > 0:
            self.overtake_counter -= 1

        ego_vehicle_loc = self._vehicle.get_location()
        ego_vehicle_wp = self._map.get_waypoint(ego_vehicle_loc)

        #1: Red lights and stops behavior

        if self.traffic_light_manager(ego_vehicle_wp) != 0:
            return self.emergency_stop()

        #2: Collision and car avoidance behaviors

        vehicle_state, vehicle, distance = self.collision_and_car_avoid_manager(ego_vehicle_wp)

        #3: Car following behaviors

        if vehicle_state:

            distance -= 2 # Discrepancy between the actual distance and the returned one.

            # Emergency brake if the car is very close.
            if distance < self.braking_distance:
                return self.emergency_stop()
            else:
                control = self.car_following_manager(vehicle, distance)

        #4: Intersection behavior

        # Checking if there's a junction nearby to slow down
        elif self.incoming_waypoint.is_junction:
            control = self._local_planner.run_step(
                target_speed=min(self.max_speed, self.speed_limit-10), debug=debug)

        #5: Normal behavior

        # Calculate controller based on no turn, traffic light or vehicle in front
        else:
            control = self._local_planner.run_step(
                target_speed=min(self.speed+(self.speed_increase_perc*self.speed)/100, \
                    min(self.max_speed, self.speed_limit-self.speed_lim_dist)),
                debug=debug)

        return control