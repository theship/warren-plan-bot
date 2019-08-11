#!/usr/bin/env python3

import json
import os
import pdb
import re
import urllib.parse

import click
import praw
import praw.models
from fuzzywuzzy import fuzz, process
from google.cloud import storage

# JSON filename of policy plans
PLANS_FILE = "plans.json"


def build_response_text(plan_record, post):
    """
    Create response text with plan summary
    """
    post_type = _post_type(post)

    submission = post if post_type == "submission" else post.submission
    comment = post if post_type == "comment" else None

    return (
        f"Senator Warren has a plan for that!"
        f"\n\n"
        f"{plan_record['summary']}"
        f"\n\n"
        # Link to learn more about the plan
        f"Learn more about her plan for [{plan_record['display_title']}]({plan_record['url']})"
        f"\n\n"
        # Horizontal line above footer
        "\n***\n"
        # Error reporting info
        f"Wrong topic or another problem?  [Send a report to my creator]"
        f"(https://www.reddit.com/message/compose?to=WarrenPlanBotDev&"
        f"subject=reference&nbsp;Submission[{submission.id}]&nbsp{'Comment[' + comment.id + ']' if comment else ''})."
        f"\n"
        # Disclaimer
        f"This bot was independently created by volunteers for Sen. Warren's 2020 campaign. "
        # Add volunteer link
        f"If you'd like to join us, visit the campaign's "
        f"[Volunteer Sign-Up Page](https://my.elizabethwarren.com/page/s/web-volunteer)."
    )


def parse_gs_uri(uri):
    '''
    :param uri:
    :return: (bucket, blob)
    '''
    parsed = urllib.parse.urlparse(uri)

    return parsed.netloc, parsed.path.strip("/")


def read_file(uri):
    if uri.startswith("gs://"):
        bucket_name, blob_name = parse_gs_uri(uri)

        storage_client = storage.Client()
        bucket = storage_client.get_bucket(bucket_name)

        return bucket.blob(blob_name).download_as_string().decode("utf-8")

    if not os.path.isfile(uri):
        return

    with open(uri, "r") as f:
        return f.read()


def write_file(uri, contents):
    if uri.startswith("gs://"):
        bucket_name, blob_name = parse_gs_uri(uri)

        storage_client = storage.Client()
        bucket = storage_client.get_bucket(bucket_name)

        return bucket.blob(blob_name).upload_from_string(contents)

    with open(uri, "w") as f:
        return f.write(contents)


def _post_type(post):
    post_class = type(post)
    if post_class is praw.models.Submission:
        return "submission"
    if post_class is praw.models.Comment:
        return "comment"
    raise NotImplementedError(f'Unsupported post type {post_class}')


def _get_text(post):
    post_class = type(post)
    if post_class is praw.models.Submission:
        return post.selftext
    if post_class is praw.models.Comment:
        return post.body
    raise NotImplementedError(f'Unsupported post type {post.__class__.__name__}')


def reply(post, reply_string: str, posts_replied_to, send=False, simulate=False):
    post_type = _post_type(post)
    if not send and not simulate:
        print(f"Bot would have replied to {post_type}: ", post.id)
        return

    if simulate:
        print(f"[simulated] Bot replying to {post_type}: ", post.id)
    else:
        post.reply(reply_string)
        print(f"Bot replying to {post_type}: ", post.id)

    posts_replied_to.append(post.id)


def process_post(post, plans_dict, posts_replied_to=None, send=False, simulate=False):
    if posts_replied_to is None:
        posts_replied_to = []

    if post.id not in posts_replied_to:

        post_text = _get_text(post)

        # Do a case insensitive search
        if re.search("!warrenplanbot|/u/WarrenPlanBot", post_text, re.IGNORECASE):
            # Initialize match_confidence and match_id before fuzzy searching
            match_confidence = 0
            match_id = 0

            # Search topic keywords and response body for best match
            for plan in plans_dict["plans"]:
                plan_match_confidence = fuzz.WRatio(post_text, plan["topic"])

                if plan_match_confidence > match_confidence:
                    # Set new match ID
                    match_confidence = plan_match_confidence
                    match_id = plan["id"]
                    print("new topic match: ", plan["topic"])

            # Select entry from plans_dict using best match ID
            plan_record = next(plan for plan in plans_dict["plans"] if plan["id"] == match_id)

            reply_string = build_response_text(plan_record, post)

            reply(post, reply_string, posts_replied_to, send=send, simulate=simulate)


click_kwargs = {
    "show_envvar": True,
    "show_default": True
}


@click.command()
@click.option('--replied-to-path', envvar='REPLIED_TO_PATH',
              type=click.Path(), default="gs://wpb-storage-dev/posts_replied_to.txt",
              help='path to file where replies are tracked', **click_kwargs)
@click.option('--send-replies/--skip-send', envvar='SEND_REPLIES',
              default=False, is_flag=True,
              help='whether to send replies', **click_kwargs)
@click.option('--skip-tracking',
              default=False, is_flag=True,
              help='whether to check whether replies have already been posted', **click_kwargs)
@click.option('--simulate-replies',
              default=False, is_flag=True,
              help='pretend to make replies, including updating state', **click_kwargs)
@click.option('--limit', envvar='LIMIT',
              type=int, default=10,
              help='number of posts to return', **click_kwargs)
@click.option('--praw-site', envvar='PRAW_SITE',
              type=click.Choice(['dev', 'prod']), default='dev',
              help='section of praw file to use for reddit module configuration', **click_kwargs)
def run_plan_bot(replied_to_path="gs://wpb-storage-dev/posts_replied_to.txt",
                 send_replies=False,
                 skip_tracking=False,
                 simulate_replies=False,
                 limit=10,
                 praw_site="dev"
                 ):
    """
    Run a single pass of Warren Plan Bot

    \b
    - Check list of posts replied to (If tracking is on)
    - Search for any new comments and submissions not on that list
    - Reply to any unreplied matching comments (If replies are on)
    - Update replied_to list (If replies and tracking is on)
    """

    if simulate_replies and send_replies:
        raise ValueError("--simulate-replies and --send-replies options are incompatible. at most one may be set")

    # Change working directory so that praw.ini works, and so all files can be in this same folder. FIXME
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    # change dev to prod to shift to production bot
    reddit = praw.Reddit(praw_site)

    with open(PLANS_FILE) as json_file:
        plans_dict = json.load(json_file)

    posts_replied_to_contents = read_file(replied_to_path) or "" if not skip_tracking else ""

    # Load the list of posts replied to or start with empty list if none
    posts_replied_to = list(filter(None, posts_replied_to_contents.split("\n")))

    # Get the subreddit
    subreddit = reddit.subreddit("WPBSandbox")

    # Get the number of new posts up to the limit
    for submission in subreddit.new(limit=limit):
        process_post(submission, plans_dict, posts_replied_to, send=send_replies, simulate=simulate_replies)

        # Get comments for submission and search for trigger in comment body
        submission.comments.replace_more(limit=None)
        for comment in submission.comments.list():
            process_post(comment, plans_dict, posts_replied_to, send=send_replies, simulate=simulate_replies)

    # Write the updated tracking list back to the file
    post_replied_to_output = "\n".join(posts_replied_to)

    if send_replies and not skip_tracking:
        write_file(replied_to_path, post_replied_to_output)
        print("updated posts_replied_to list:", "\n", post_replied_to_output)
    else:
        print("would have updated posts_replied_to list to:", "\n", post_replied_to_output)


def run_plan_bot_event_handler(event, context):
    # Click exits with return code 0 when everything worked. Skip that behavior
    try:
        run_plan_bot(prog_name='run_that_plan_bot')  # need to set prog_name to avoid weird click behavior in cloud fn
    except SystemExit as e:
        if e.code != 0:
            raise


if __name__ == "__main__":
    run_plan_bot()
