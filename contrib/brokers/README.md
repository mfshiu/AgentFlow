# Unfinished broker drafts

These files were moved out of the `agentflow` package by issue #42.
They are early drafts, kept for reference, and they do not run:

- `dds_broker.py` needs RTI Connext (`rticonnextdds_connector`) and
  still refers to an MQTT client (`Client`, `self._client`) that it
  never creates.
- `ros_noetic_broker.py` needs ROS Noetic (`rospy`, `std_msgs`). It
  imports `LOGGER_NAME` from the package root, which does not define
  it, and publishes ROS messages where the agent passes bytes.

Both still use relative imports from `agentflow.broker`, so they cannot
be imported from here. A real DDS or ROS backend should wait for the
transport interface planned in issue #69, and ship as an optional
plug-in with its own tests.

Today only the MQTT broker carries messages. `broker_type: redis` and
`broker_type: ros` are reserved names: an agent that selects either
stops at start with an error.
