import sys
from torch import multiprocessing
from torch.utils.tensorboard.writer import SummaryWriter
from continual_rl.utils.argparse_manager import ArgparseManager


if __name__ == "__main__":
    # Pytorch multiprocessing requires either forkserver or spawn.
    try:
        multiprocessing.set_start_method("forkserver")
    except ValueError as e:
        # Windows doesn't support forking, so fall back to spawn instead
        assert "cannot find context" in str(e)
        multiprocessing.set_start_method("spawn")

    experiment, policy = ArgparseManager.parse(sys.argv[1:])
    summary_writer = SummaryWriter(log_dir=experiment.output_dir)
    experiment.try_run(policy, summary_writer=summary_writer)