import re

from google.cloud import firestore

from matching import RuleStrategy, Strategy


def footer(post):
    return (
        f"\n\n"
        # Horizontal line above footer
        "\n***\n"
        # Error reporting info
        f"Wrong topic or another problem?  [Send a report to my creator]"
        f"(https://www.reddit.com/message/compose?to=WarrenPlanBotDev&"
        f"subject=BotReport&"
        f"message=Issue with bot response to: {post.permalink}).  "
        f"\n"
        # Disclaimer
        f"This bot was independently created by volunteers for Sen. Warren's 2020 campaign.  "
        f"\n"
        # Add volunteer link
        f"If you'd like to join us, visit the campaign's "
        f"[Volunteer Sign-Up Page](https://my.elizabethwarren.com/page/s/web-volunteer)."
    )


def _plan_links(plans):
    return "\n".join(
        ["[" + plan["display_title"] + "](" + plan["url"] + ")  " for plan in plans]
    )


def build_response_text_plan_cluster(plan_record, post):
    """
    Create response text with plan summary when plan is actually a plan cluster
    """

    return (
        f"Senator Warren has quite a number of plans for that!"
        f"\n\n"
        # Links to learn more about the plan cluster
        f"Learn more about her plans for {plan_record['display_title']}:"
        f"\n\n"
        f"{ _plan_links(plan_record['plans'])}"
        f"{footer(post)}"
    )


def build_response_text_pure_plan(plan_record, post):
    """
    Create response text with plan summary
    """

    return (
        f"Senator Warren has a plan for that!"
        f"\n\n"
        f"{plan_record['summary']}"
        f"\n\n"
        # Link to learn more about the plan
        f"Learn more about her plan: [{plan_record['display_title']}]({plan_record['url']})"
        f"{footer(post)}"
    )


def build_response_text(plan_record, post):
    if plan_record.get("is_cluster"):
        return build_response_text_plan_cluster(plan_record, post)
    return build_response_text_pure_plan(plan_record, post)


def build_no_match_response_text(potential_plan_matches, post):
    if potential_plan_matches:
        return (
            f"I'm not sure exactly which plan you're looking for! "
            f"My best guesses for what you were asking about are:"
            f"\n\n"
            f"{ _plan_links(match['plan'] for match in potential_plan_matches[:8])}"
            f"\n\n"
            f"Or I can show you my full list of her plans if you reply with"
            f"\n\n"
            f"!WarrenPlanBot show me the plans"
            f"\n\n"
            f"{footer(post)}"
        )
    else:
        return (
            f"I'm not sure exactly which plan you're looking for, "
            f"and I'm not feeling confident enough in any of my guesses to tell you about them! ':("
            f"\n\n"
            f"I can show you my full list of her plans if you reply with"
            f"\n\n"
            f"!WarrenPlanBot show me the plans"
            f"\n\n"
            f"Or please kindly rephrase? ':D"
            f"{footer(post)}"
        )


def build_all_plans_response_text(plans, post):
    pure_plans = list(filter(lambda p: not p.get("is_cluster"), plans))

    response = (
        f"Here is the full list of plans that Sen. Warren has released so far:"
        f"\n\n"
        f"|[{pure_plans[0]['display_title']}]({pure_plans[0]['url']})|[{pure_plans[1]['display_title']}]({pure_plans[1]['url']})|[{pure_plans[2]['display_title']}]({pure_plans[2]['url']})|"
        f"\n"
        f"|:-:|:-:|:-:|"
        f"\n"
    )
    for i, plan in enumerate(pure_plans[3:], start=3):
        response += f"|[{plan['display_title']}]({plan['url']})"
        if (i + 1) % 3:
            response += "|\n"

    response += f"\n\n" f"{footer(post)}"

    return response


def reply(post, reply_string: str, send=False, simulate=False):
    """
    :param post: post to reply on
    :param reply_string: string to reply with
    :param send: whether to send an actual reply to reddit
    :param simulate: whether to simulate sending an actual reply to reddit
    :return: did_reply – whether an actual or simulated reply was made
    """

    if simulate:
        print(f"[simulated] Bot replying to {post.type}: ", post.id)
        return True
    if send:
        post.reply(reply_string)
        return True

    print(f"Bot would have replied to {post.type}: ", post.id)


def process_post(
    post,
    plans,
    posts_db,
    post_ids_replied_to=None,
    send=False,
    simulate=False,
    skip_tracking=False,
    matching_strategy=Strategy.lsa_gensim_v2,
):
    if post_ids_replied_to is None:
        post_ids_replied_to = []

    if (
        # Never try to reply if a post is locked
        post.locked
        # Never reply to a deleted post
        or not post.author
        # Make sure we're not replying to ourself
        or "warrenplanbot" in post.author.name.lower()
        # Make sure we don't reply to a post we've already replied to
        or post.id in post_ids_replied_to
    ):
        return

    # Ensure it's a post where someone summoned us
    if not re.search("!warrenplanbot|/u/WarrenPlanBot", post.text, re.IGNORECASE):
        return

    match_info = (
        RuleStrategy.request_plan_list(plans, post)
        or RuleStrategy.match_display_title(plans, post)
        or matching_strategy(plans, post)
    )

    match = match_info["match"]
    operation = match_info["operation"]
    plan_confidence = match_info["confidence"]
    plan = match_info["plan"]
    potential_matches = match_info.get("potential_matches")
    plan_id = plan["id"]

    # Create partial db entry from known values, placeholder defaults for mutable values
    db_data = create_db_record(post, match, plan_confidence, plan_id)

    # If plan is matched with confidence, build and send reply
    if match:
        print("plan match: ", plan_id, post.id, plan_confidence)

        reply_string = build_response_text(plan, post)
        db_data["reply_type"] = "plan_cluster" if plan.get("is_cluster") else "plan"
    elif operation == "all_the_plans":
        print("all the plans requested: ", post.id)

        reply_string = build_all_plans_response_text(plans, post)
        db_data["reply_type"] = "operation"
        db_data["operation"] = "all_the_plans"
    else:
        print("topic mismatch: ", plan_id, post.id, plan_confidence)

        reply_string = build_no_match_response_text(potential_matches, post)
        db_data["reply_type"] = "no_match"

    did_reply = reply(post, reply_string, send=send, simulate=simulate)

    if did_reply and not skip_tracking:
        # Replace default None values in db_data record
        db_data["replied"] = True
        db_data["reply_timestamp"] = firestore.SERVER_TIMESTAMP

        posts_db.document(post.id).set(db_data)


def create_db_record(
    post, match, plan_confidence, plan_id, reply_timestamp=None, reply_made=False
) -> dict:
    # Reddit 3-digit code prefix removed for each id, leaving only the ID itself
    post_parent_id = post.parent_id[3:] if post.type == "comment" else None
    post_subreddit_id = post.subreddit.name[3:]
    post_top_level_parent_id = post.link_id[3:] if post.type == "comment" else None
    post_title = post.title if post.type == "submission" else None
    # Return db_entry for Firestore
    entry = {
        "replied": reply_made,
        "type": post.type,
        "post_id": post.id,
        "post_author": "/u/" + post.author.name,
        "post_text": post.text,
        "post_parent_id": post_parent_id,  # ID or None if no parent_id
        "post_url": "https://www.reddit.com" + post.permalink,
        "post_subreddit_id": post_subreddit_id,
        "post_subreddit_display_name": post.subreddit.display_name,
        "post_title": post_title,  # Post Title or None if no title
        "post_top_level_parent_id": post_top_level_parent_id,
        "post_locked": post.locked,
        # TODO flesh out / clarify this some
        "plan_match": match,
        "top_plan_confidence": plan_confidence,
        "top_plan": plan_id,
        "reply_timestamp": reply_timestamp,
    }

    return entry
