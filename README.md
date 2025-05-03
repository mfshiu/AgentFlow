# AgentFlow

**Resilient Adaptive Cloud-Edge Framework for Multi-Agent Coordination**

AgentFlow is a Multi-Agent System (MAS)-based framework for scalable, fault-tolerant coordination across heterogeneous cloud-edge infrastructures. Designed for real-time and mission-critical applications, AgentFlow enables decentralized, dynamic service orchestration using programmable logistics objects and abstract agent interfaces.

## 🔍 Key Features

- **Decentralized Decision Making**  
  Supports agent-driven coordination without central servers using lightweight consensus.

- **Programmable Logistics Objects**  
  Request/Response Logistics allow precise, selective communication, minimizing unnecessary network traffic.

- **Dynamic Service Election**  
  Agents elect optimal services at runtime based on real-time load and responsiveness.

- **Many-to-Many Coordination Model**  
  Enables scalable task distribution and autonomous recovery in complex cloud-edge environments.

- **Resilience and Fault Tolerance**  
  Supports agent-level fault containment and task reassignment under node failures.

- **Modular Architecture**  
  Holonic agent structure with loosely coupled communication layers (e.g., MQTT, DDS).

## 🧱 Architecture Overview

```
+----------------------------+
|     Orchestration Layer    | <-- Load balancing, no central control
+----------------------------+
|         Agent Layer        | <-- Holonic agents: Perception, Decision, Action
+----------------------------+
|     Communication Layer    | <-- MQTT / DDS pub-sub abstraction
+----------------------------+
```

## 🧠 How It Works

AgentFlow employs an event-driven publish-subscribe pattern with three logistics mechanisms:

- **Selective Request-Response**: Each client gets a unique topic to prevent message broadcasting.
- **Dynamic Election**: Tasks are dynamically assigned to the least-loaded agent.
- **Composite Coordination**: Coordinators manage agent clusters for many-to-many interactions.

### Example Algorithms

**Service Agent Selection:**
```math
s^* = \arg\min_{s \in S} \text{Load}(s)
```

**Communication Mapping:**
```math
f: c_i \rightarrow t_i
```

## 📊 Experimental Results

Tested using a swarm of 50–500 autonomous mobile robots (AMRs):

| Metric                    | Result            |
|---------------------------|-------------------|
| Task Success Rate         | 98.5%             |
| Task Assignment Latency   | 30–63 ms          |
| Election Convergence Time | ~18 ms            |
| MTTR under failure        | < 30 sec          |
| Orphaned Tasks (30% fail) | 14 (of 1000+)     |

## 🚀 Applications

- Smart warehouses and AMR fleets
- Industrial IoT and edge robotics
- Intelligent grid and healthcare logistics
- Programmable, real-time distributed systems

## 📦 Repository Structure

```bash
AgentFlow/
├── src/agentflow/
│          ├── broker/    # MQTT/DDS brokers
│          ├── core/    # Holonic agent definitions
│          └── logistics/    # Request/response/election logistics
├── unittest/    # AgentFlow unittests
└── README.md
```

## 📬 Contact

For questions or collaboration inquiries, contact:

- Ming Fang Shiu – [108582003@cc.ncu.edu.tw](mailto:108582003@cc.ncu.edu.tw)
- Prof. Ching Han Chen – [pierre@csie.ncu.edu.tw](mailto:pierre@csie.ncu.edu.tw)
