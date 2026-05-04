BALLPARK_HR_FACTORS = {
    "Coors Field": 1.36,
    "Great American Ball Park": 1.23,
    "Citizens Bank Park": 1.18,
    "Yankee Stadium": 1.17,
    "American Family Field": 1.15,
    "Busch Stadium": 1.14,
    "Fenway Park": 1.13,
    "Oriole Park at Camden Yards": 1.12,
    "Truist Park": 1.11,
    "Guaranteed Rate Field": 1.10,
    "Globe Life Field": 1.09,
    "Target Field": 1.08,
    "Progressive Field": 1.07,
    "Comerica Park": 1.06,
    "Wrigley Field": 1.05,
    "Kauffman Stadium": 1.04,
    "Dodger Stadium": 1.02,
    "Rogers Centre": 1.01,
    "LoanDepot Park": 1.00,
    "Tropicana Field": 0.99,
    "PNC Park": 0.98,
    "Citi Field": 0.97,
    "Angel Stadium": 0.96,
    "Chase Field": 0.95,
    "Nationals Park": 0.94,
    "Minute Maid Park": 0.93,
    "Oakland Coliseum": 0.91,
    "T-Mobile Park": 0.87,
    "Oracle Park": 0.88,
}


def get_hr_factor(venue_name: str) -> float:
    for name, factor in BALLPARK_HR_FACTORS.items():
        if name.lower() in venue_name.lower() or venue_name.lower() in name.lower():
            return factor
    return 1.00
