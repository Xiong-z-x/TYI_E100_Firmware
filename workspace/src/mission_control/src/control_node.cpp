#include <ros/ros.h>
#include <std_srvs/SetBool.h>

#include "mission_control/flight_interface.h"

namespace {

class ControlService {
public:
  explicit ControlService(mission_control::FlightInterface& flight) : flight_(flight) {}

  bool bootCallback(std_srvs::SetBool::Request& request, std_srvs::SetBool::Response& response) {
    if (request.data) {
      response.success = flight_.boot();
      response.message = response.success ? "mission control booted" : "mission control boot failed";
      return true;
    }

    flight_.lock();
    response.success = true;
    response.message = "mission control locked";
    return true;
  }

private:
  mission_control::FlightInterface& flight_;
};

}  // namespace

int main(int argc, char** argv) {
  ros::init(argc, argv, "mission_control_node");
  ros::NodeHandle nh("~");
  mission_control::FlightInterface flight(nh);
  ControlService control_service(flight);

  ros::ServiceServer boot_service = nh.advertiseService("boot", &ControlService::bootCallback, &control_service);
  ROS_INFO("mission_control_node started; boot service: %s/boot", ros::this_node::getName().c_str());

  ros::Rate rate(1.0);
  while (ros::ok()) {
    ros::spinOnce();
    if (flight.ready()) {
      ROS_INFO_STREAM_THROTTLE(5.0, "control status: " << flight.statusText());
    }
    rate.sleep();
  }
  return 0;
}
