from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


def start_scheduler(app):
    """Start background scheduler that refreshes prices daily at 09:00."""
    scheduler = BackgroundScheduler(daemon=True)

    def update_job():
        with app.app_context():
            from stocks import update_all_prices
            update_all_prices()

    # Every day at 09:00
    scheduler.add_job(
        update_job,
        CronTrigger(hour=9, minute=0),
        id='daily_price_update',
        replace_existing=True,
    )

    scheduler.start()
    print("Scheduler started — prices update daily at 09:00.")
    return scheduler
