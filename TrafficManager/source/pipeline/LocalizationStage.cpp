#include "LocalizationStage.h"

namespace traffic_manager {

namespace LocalizationConstants {
  static const float WAYPOINT_TIME_HORIZON = 3.0f;
  static const float MINIMUM_HORIZON_LENGTH = 25.0f;
  static const float TARGET_WAYPOINT_TIME_HORIZON = 0.5f;
  static const float TARGET_WAYPOINT_HORIZON_LENGTH = 2.0f;
  static const float MINIMUM_JUNCTION_LOOK_AHEAD = 3.0f;
  static const float HIGHWAY_SPEED = 50 / 3.6f;
}
  using namespace LocalizationConstants;

  LocalizationStage::LocalizationStage(
      std::shared_ptr<LocalizationToPlannerMessenger> planner_messenger,
      std::shared_ptr<LocalizationToCollisionMessenger> collision_messenger,
      std::shared_ptr<LocalizationToTrafficLightMessenger> traffic_light_messenger,
      uint number_of_vehicles,
      uint pool_size,
      std::vector<Actor> &actor_list,
      InMemoryMap &local_map,
      cc::DebugHelper &debug_helper)
    : planner_messenger(planner_messenger),
      collision_messenger(collision_messenger),
      traffic_light_messenger(traffic_light_messenger),
      actor_list(actor_list),
      local_map(local_map),
      debug_helper(debug_helper),
      PipelineStage(pool_size, number_of_vehicles) {

    // Initializing various output frame selectors.
    planner_frame_selector = true;
    collision_frame_selector = true;
    traffic_light_frame_selector = true;
    // Allocating the buffer lists.
    buffer_list_a = std::make_shared<BufferList>(number_of_vehicles);
    buffer_list_b = std::make_shared<BufferList>(number_of_vehicles);
    // Allocating output frames to be shared with the motion planner stage.
    planner_frame_a = std::make_shared<LocalizationToPlannerFrame>(number_of_vehicles);
    planner_frame_b = std::make_shared<LocalizationToPlannerFrame>(number_of_vehicles);
    // Allocating output frames to be shared with the collision stage.
    collision_frame_a = std::make_shared<LocalizationToCollisionFrame>(number_of_vehicles);
    collision_frame_b = std::make_shared<LocalizationToCollisionFrame>(number_of_vehicles);
    // Allocating output frames to be shared with the traffic light stage
    traffic_light_frame_a = std::make_shared<LocalizationToTrafficLightFrame>(number_of_vehicles);
    traffic_light_frame_b = std::make_shared<LocalizationToTrafficLightFrame>(number_of_vehicles);
    // Initializing messenger states to initiate data writes
    // preemptively since this is the first stage in the pipeline.
    planner_messenger_state = planner_messenger->GetState() - 1;
    collision_messenger_state = collision_messenger->GetState() - 1;
    traffic_light_messenger_state = traffic_light_messenger->GetState() - 1;

    // Connecting vehicle ids to their position indices on data arrays.
    uint index = 0u;
    for (auto &actor: actor_list) {
      vehicle_id_to_index.insert({actor->GetId(), index});
      ++index;
    }
  }

  LocalizationStage::~LocalizationStage() {}

  void LocalizationStage::Action(const uint start_index, const uint end_index) {

    // Selecting output frames based on selector keys.
    auto current_planner_frame = planner_frame_selector ? planner_frame_a : planner_frame_b;
    auto current_collision_frame = collision_frame_selector ? collision_frame_a : collision_frame_b;
    auto current_traffic_light_frame =
        traffic_light_frame_selector ? traffic_light_frame_a : traffic_light_frame_b;
    auto current_buffer_list = collision_frame_selector ? buffer_list_a : buffer_list_b;
    auto copy_buffer_list = !collision_frame_selector ? buffer_list_a : buffer_list_b;

    // Looping over arrays' partitions for the current thread.
    for (uint i = start_index; i <= end_index; ++i) {

      Actor vehicle = actor_list.at(i);
      ActorId actor_id = vehicle->GetId();

      cg::Location vehicle_location = vehicle->GetLocation();
      float vehicle_velocity = vehicle->GetVelocity().Length();

      float horizon_size = std::max(
          WAYPOINT_TIME_HORIZON * vehicle_velocity,
          MINIMUM_HORIZON_LENGTH);

      Buffer &waypoint_buffer = current_buffer_list->at(i);
      Buffer &copy_waypoint_buffer = copy_buffer_list->at(i);

      // Synchronizing buffer copies in case the path of the vehicle has changed.
      if (!waypoint_buffer.empty() && !copy_waypoint_buffer.empty() &&
          ((copy_waypoint_buffer.front()->GetWaypoint()->GetLaneId()
          != waypoint_buffer.front()->GetWaypoint()->GetLaneId()) ||
          (copy_waypoint_buffer.front()->GetWaypoint()->GetSectionId()
          != waypoint_buffer.front()->GetWaypoint()->GetSectionId()) ||
          (copy_waypoint_buffer.front()->GetWaypoint()->GetRoadId()
          != waypoint_buffer.front()->GetWaypoint()->GetRoadId()))) {

        waypoint_buffer.clear();
        waypoint_buffer.assign(copy_waypoint_buffer.begin(), copy_waypoint_buffer.end());
      }

      // Purge passed waypoints.
      if (!waypoint_buffer.empty()) {

        float dot_product = DeviationDotProduct(vehicle, waypoint_buffer.front()->GetLocation());

        while (dot_product <= 0 && !waypoint_buffer.empty()) {
          waypoint_buffer.pop_front();
          if (!waypoint_buffer.empty()) {
            dot_product = DeviationDotProduct(vehicle, waypoint_buffer.front()->GetLocation());
          }
        }
      }

      // Initializing buffer if it is empty.
      if (waypoint_buffer.empty()) {
        SimpleWaypointPtr closest_waypoint = local_map.GetWaypoint(vehicle_location);
        waypoint_buffer.push_back(closest_waypoint);
      }

      // Assign a lane change.
      SimpleWaypointPtr front_waypoint = waypoint_buffer.front();
      GeoIds current_road_ids = {
        front_waypoint->GetWaypoint()->GetRoadId(),
        front_waypoint->GetWaypoint()->GetSectionId(),
        front_waypoint->GetWaypoint()->GetLaneId()
      };

      traffic_distributor.UpdateVehicleRoadPosition(
          actor_id,
          current_road_ids);

      if (!front_waypoint->CheckJunction()) {
        SimpleWaypointPtr change_over_point = traffic_distributor.AssignLaneChange(
            vehicle,
            front_waypoint,
            current_road_ids,
            current_buffer_list,
            vehicle_id_to_index,
            actor_list,
            debug_helper);

        if (change_over_point != nullptr) {
          waypoint_buffer.clear();
          waypoint_buffer.push_back(change_over_point);
        }
      }

      // Populating the buffer.
      while (waypoint_buffer.back()->DistanceSquared(waypoint_buffer.front())
             <= std::pow(horizon_size, 2)) {

        uint pre_selection_id = waypoint_buffer.back()->GetWaypoint()->GetId();
        std::vector<SimpleWaypointPtr> next_waypoints = waypoint_buffer.back()->GetNextWaypoint();

        uint selection_index = 0u;
        // Pseudo-randomized path selection if found more than one choice.
        if (next_waypoints.size() > 1) {
          selection_index = rand() % next_waypoints.size();
        }

        waypoint_buffer.push_back(next_waypoints.at(selection_index));
      }

      // Generating output.
      float target_point_distance = std::max(std::ceil(vehicle_velocity * TARGET_WAYPOINT_TIME_HORIZON),
                                             TARGET_WAYPOINT_HORIZON_LENGTH);
      SimpleWaypointPtr target_waypoint = waypoint_buffer.front();
      for (uint i = 0u;
          (i < waypoint_buffer.size()) &&
          (waypoint_buffer.front()->DistanceSquared(target_waypoint)
          < std::pow(target_point_distance, 2));
          ++i) {
        target_waypoint = waypoint_buffer.at(i);
      }
      cg::Location target_location = target_waypoint->GetLocation();
      float dot_product = DeviationDotProduct(vehicle, target_location);
      float cross_product = DeviationCrossProduct(vehicle, target_location);
      dot_product = 1 - dot_product;
      if (cross_product < 0) {
        dot_product *= -1;
      }

      // Filtering out false junctions on highways.
      // On highways, if there is only one possible path and the section is
      // marked as intersection, ignore it.
      auto vehicle_reference = boost::static_pointer_cast<cc::Vehicle>(vehicle);
      float speed_limit = vehicle_reference->GetSpeedLimit();
      float look_ahead_distance = std::max(2 * vehicle_velocity, MINIMUM_JUNCTION_LOOK_AHEAD);

      SimpleWaypointPtr look_ahead_point = waypoint_buffer.front();
      uint look_ahead_index = 0u;
      for (uint i = 0u;
          (waypoint_buffer.front()->DistanceSquared(look_ahead_point)
          < std::pow(look_ahead_distance, 2)) &&
          (i < waypoint_buffer.size());
          ++i) {
        look_ahead_point = waypoint_buffer.at(i);
        look_ahead_index = i;
      }

      bool approaching_junction = false;
      if (look_ahead_point->CheckJunction() && !(waypoint_buffer.front()->CheckJunction())) {
        if (speed_limit > HIGHWAY_SPEED) {
          for (uint i = 0u; (i < look_ahead_index) && !approaching_junction; ++i) {
            SimpleWaypointPtr swp = waypoint_buffer.at(i);
            if (swp->GetNextWaypoint().size() > 1) {
              approaching_junction = true;
            }
          }
        } else {
          approaching_junction = true;
        }
      }

      // Editing output frames.
      LocalizationToPlannerData &planner_message = current_planner_frame->at(i);
      planner_message.actor = vehicle;
      planner_message.deviation = dot_product;
      planner_message.approaching_true_junction = approaching_junction;

      LocalizationToCollisionData &collision_message = current_collision_frame->at(i);
      collision_message.actor = vehicle;
      collision_message.buffer = &waypoint_buffer;

      LocalizationToTrafficLightData &traffic_light_message = current_traffic_light_frame->at(i);
      traffic_light_message.actor = vehicle;
      traffic_light_message.closest_waypoint = waypoint_buffer.front();
      traffic_light_message.junction_look_ahead_waypoint = waypoint_buffer.at(look_ahead_index);
    }
  }

  void LocalizationStage::DataReceiver() {}

  void LocalizationStage::DataSender() {

    // Since send/receive calls on messenger objects can block if the other
    // end hasn't received/sent data, choose to block on only those stages
    // which takes the most priority (which needs the highest rate of data feed)
    // to run the system well.

    DataPacket<std::shared_ptr<LocalizationToPlannerFrame>> planner_data_packet = {
      planner_messenger_state,
      planner_frame_selector ? planner_frame_a : planner_frame_b
    };
    planner_frame_selector = !planner_frame_selector;
    planner_messenger_state = planner_messenger->SendData(planner_data_packet);

    // Send data to collision stage only if it has finished
    // processing, received the previous message and started processing it.
    int collision_messenger_current_state = collision_messenger->GetState();
    if (collision_messenger_current_state != collision_messenger_state) {
      DataPacket<std::shared_ptr<LocalizationToCollisionFrame>> collision_data_packet = {
        collision_messenger_state,
        collision_frame_selector ? collision_frame_a : collision_frame_b
      };

      collision_messenger_state = collision_messenger->SendData(collision_data_packet);
      collision_frame_selector = !collision_frame_selector;
    }

    // Send data to traffic light stage only if it has finished
    // processing, received the previous message and started processing it.
    int traffic_light_messenger_current_state = traffic_light_messenger->GetState();
    if (traffic_light_messenger_current_state != traffic_light_messenger_state) {
      DataPacket<std::shared_ptr<LocalizationToTrafficLightFrame>> traffic_light_data_packet = {
        traffic_light_messenger_state,
        traffic_light_frame_selector ? traffic_light_frame_a : traffic_light_frame_b
      };

      traffic_light_messenger_state = traffic_light_messenger->SendData(traffic_light_data_packet);
      traffic_light_frame_selector = !traffic_light_frame_selector;
    }
  }

  void LocalizationStage::DrawBuffer(Buffer &buffer) {

    for (int i = 0; i < buffer.size() && i < 5; ++i) {
      debug_helper.DrawPoint(buffer.at(i)->GetLocation(), 0.1f, {255u, 0u, 0u}, 0.5f);
    }
  }
}
