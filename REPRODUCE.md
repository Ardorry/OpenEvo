# Reproduce ChemCrowRun

Follow the [complete installation, deployment, experiment and evaluation guide](benchmarks/chemcrow/REPRODUCE.md).
The experiment entrypoint is `chemcrowrun`, and fresh results live in `outputs/chemcrowrun/<run_id>/`.

ChemCrowRun v1.0 preserves the full-v8 experimental procedure and immutable historical evidence.
An explicitly declared model substitution is a separate comparison condition. This is an independent
reproduction implementation, not an official ChemCrow leaderboard certification.

The [delivery inventory](benchmarks/chemcrow/DELIVERY.md) lists the required source, configuration,
results, evolution artifacts and runtime downloads. This fork includes the Core patch; separate
upstream installations must follow [the patch instructions](benchmarks/chemcrow/patches/README.md).
The completed local experiment is distinct from pending public remote fresh-clone paid acceptance.
