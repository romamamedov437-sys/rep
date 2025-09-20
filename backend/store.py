class _Memory:
    def __init__(self):
        self.jobs = {}  # job_id -> {status, progress, user_id, model_id, replicate_training_id}

memory = _Memory()
