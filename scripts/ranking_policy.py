def first_known(*values):
    for value in values:
        if value is not None:
            return value
    return None


def revenue_growth_score(growth, price_location=None, loss_improving=None, high=70, mid=75, base=30, early=5, flat=0):
    if growth is None:
        return 0, None
    if 20 <= growth < 30:
        score = high
        note = "売上20%台・2倍到達率重視"
    elif 15 <= growth < 20:
        score = mid
        note = "売上15-19%台・利確成績重視"
    elif growth >= 30:
        return -10, "売上30%超・過熱注意"
    elif growth >= 10:
        return base, "売上成長 (+10%↑)"
    elif growth >= 5:
        return early, "売上兆し (+5%↑)"
    elif growth >= 0:
        return flat, "売上維持"
    elif growth < -10:
        return -90, "売上悪化 (-10%↓)"
    else:
        return -40, "売上減少"

    if price_location is not None:
        if price_location < 0.15:
            score += 25
            note += "+底値圏"
        elif price_location < 0.3:
            score += 10
        elif price_location > 0.7:
            score -= 70
            note += "・高値圏で減点"
        elif price_location > 0.5:
            score -= 35
            note += "・中高値圏で減点"

    if loss_improving is True:
        score += 15
        note += "+赤字縮小"
    elif loss_improving is False:
        score -= 20
        note += "・赤字悪化で減点"

    return score, note
