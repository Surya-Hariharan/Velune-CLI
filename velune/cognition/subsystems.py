from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_cognitive_firewall(env: RuntimeEnvironment):
    from velune.cognition.firewall import CognitiveFirewall

    return CognitiveFirewall()


COGNITION_MODULES = [
    SubsystemModule(
        name="cognitive_firewall",
        factory=_create_cognitive_firewall,
        container_key="runtime.firewall",
    ),
]
