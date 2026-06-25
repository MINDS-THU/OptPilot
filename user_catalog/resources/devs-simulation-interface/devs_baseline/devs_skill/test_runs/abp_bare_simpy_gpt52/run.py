import simpy
import json
import sys
import argparse


def log_event(env, entity, event, payload):
    record = {
        "time": float(env.now),
        "entity": entity,
        "event": event,
        "payload": payload,
    }
    print(json.dumps(record), file=sys.stdout, flush=True)


class Subnet:
    def __init__(self, env, channel_name, seed, channel_delay):
        self.env = env
        self.channel_name = channel_name
        self.x = seed
        self.channel_delay = channel_delay
        self.input_store = simpy.Store(env)
        self.output_store = simpy.Store(env)

    def process(self):
        while True:
            packet = yield self.input_store.get()
            x_new = (17 * self.x + 11) % 100
            self.x = x_new
            if x_new < 10:
                behavior = "drop"
            else:
                behavior = "pass"
            log_event(
                self.env,
                "subnet",
                "packet_get",
                {
                    "behavior": behavior,
                    "channel": self.channel_name,
                    "noise_value": x_new,
                },
            )
            if behavior == "pass":
                yield self.env.timeout(self.channel_delay)
                yield self.output_store.put(packet)


class Receiver:
    def __init__(self, env, receiver_delay, incoming_store, ack_output_store):
        self.env = env
        self.receiver_delay = receiver_delay
        self.incoming_store = incoming_store
        self.ack_output_store = ack_output_store

    def process(self):
        buffer = []
        get_event = self.incoming_store.get()

        while True:
            if buffer:
                pkt = buffer.pop(0)
            else:
                pkt = yield get_event
                get_event = self.incoming_store.get()

            log_event(
                self.env,
                "receiver",
                "delay_start",
                {"type": "processing", "duration": float(self.receiver_delay)},
            )

            proc_done = self.env.timeout(self.receiver_delay)

            while True:
                result = yield proc_done | get_event
                if get_event in result:
                    if len(buffer) < 1:
                        buffer.append(result[get_event])
                    get_event = self.incoming_store.get()
                else:
                    break

            log_event(
                self.env,
                "receiver",
                "packet_received",
                {"seq_num": pkt["seq_num"], "bit": pkt["bit"]},
            )
            yield self.ack_output_store.put({"bit": pkt["bit"]})


class Sender:
    def __init__(self, env, total_packets, sender_delay, timeout,
                 data_output_store, ack_input_store):
        self.env = env
        self.total_packets = total_packets
        self.sender_delay = sender_delay
        self.timeout = timeout
        self.data_output_store = data_output_store
        self.ack_input_store = ack_input_store
        self.ack_event = simpy.Event(env)
        self.ack_data = None

    def ack_listener(self):
        while True:
            ack = yield self.ack_input_store.get()
            self.ack_data = ack
            if not self.ack_event.triggered:
                self.ack_event.succeed()

    def process(self):
        for seq_num in range(1, self.total_packets + 1):
            bit = 0 if (seq_num % 2 == 1) else 1

            log_event(
                self.env,
                "sender",
                "delay_start",
                {"type": "preparation", "duration": float(self.sender_delay)},
            )
            yield self.env.timeout(self.sender_delay)

            is_retry = False
            while True:
                packet = {"seq_num": seq_num, "bit": bit}
                yield self.data_output_store.put(packet)
                log_event(
                    self.env,
                    "sender",
                    "packet_sent",
                    {
                        "seq_num": seq_num,
                        "bit": bit,
                        "is_retry": is_retry,
                    },
                )

                self.ack_event = simpy.Event(self.env)
                timeout_event = self.env.timeout(self.timeout)

                result = yield timeout_event | self.ack_event

                if self.ack_event in result:
                    ack = self.ack_data
                    ack_bit = ack["bit"]
                    is_valid = ack_bit == bit
                    log_event(
                        self.env,
                        "sender",
                        "ack_received",
                        {"ack_bit": ack_bit, "is_valid": is_valid},
                    )
                    if is_valid:
                        break
                is_retry = True


def main():
    parser = argparse.ArgumentParser(description="ABP Simulation with simpy")
    parser.add_argument("--total_packets", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sender_delay", type=int, default=10)
    parser.add_argument("--receiver_delay", type=int, default=10)
    parser.add_argument("--channel_delay", type=int, default=3)
    parser.add_argument("--simulate_time", type=float, default=1000)
    args = parser.parse_args()

    env = simpy.Environment()

    subnet1 = Subnet(env, "forward", args.seed, args.channel_delay)
    subnet2 = Subnet(env, "backward", args.seed, args.channel_delay)

    receiver = Receiver(env, args.receiver_delay, subnet1.output_store,
                        subnet2.input_store)
    sender = Sender(env, args.total_packets, args.sender_delay, args.timeout,
                    subnet1.input_store, subnet2.output_store)

    env.process(subnet1.process())
    env.process(subnet2.process())
    env.process(receiver.process())
    env.process(sender.ack_listener())
    env.process(sender.process())

    env.run(until=args.simulate_time)


if __name__ == "__main__":
    main()
