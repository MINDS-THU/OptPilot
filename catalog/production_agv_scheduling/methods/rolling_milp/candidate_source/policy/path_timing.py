# config/path_timing.py
"""
Path segment timing configuration for AGV navigation.
Defines the time required to travel between specific path points.
This file is auto-generated from AGVPathInfos.json by tools/process_path_data.py
"""

from typing import Dict, Tuple

# Path segment timing hashtable
# Key: (point_from, point_to) tuple
# Value: travel time in seconds
PATH_SEGMENT_TIMES: Dict[Tuple[str, str], float] = {
    ("P0", "P1"): 4.71404521,
    ("P0", "P2"): 6.66666667,
    ("P0", "P3"): 8.33333333,
    ("P0", "P4"): 10.00000000,
    ("P0", "P5"): 11.66666667,
    ("P0", "P6"): 13.33333333,
    ("P0", "P7"): 15.00000000,
    ("P0", "P8"): 15.83333333,
    ("P0", "P9"): 18.33333333,
    ("P0", "P10"): 13.33333333,
    ("P1", "P2"): 4.71404521,
    ("P1", "P3"): 6.66666667,
    ("P1", "P4"): 8.33333333,
    ("P1", "P5"): 10.00000000,
    ("P1", "P6"): 11.66666667,
    ("P1", "P7"): 13.33333333,
    ("P1", "P8"): 14.16666667,
    ("P1", "P9"): 16.66666667,
    ("P1", "P10"): 11.66666667,
    ("P2", "P3"): 4.71404521,
    ("P2", "P4"): 6.66666667,
    ("P2", "P5"): 8.33333333,
    ("P2", "P6"): 10.00000000,
    ("P2", "P7"): 11.66666667,
    ("P2", "P8"): 12.50000000,
    ("P2", "P9"): 15.00000000,
    ("P2", "P10"): 10.00000000,
    ("P3", "P4"): 4.71404521,
    ("P3", "P5"): 6.66666667,
    ("P3", "P6"): 8.33333333,
    ("P3", "P7"): 10.00000000,
    ("P3", "P8"): 10.83333333,
    ("P3", "P9"): 13.33333333,
    ("P3", "P10"): 8.04737854,
    ("P4", "P5"): 4.71404521,
    ("P4", "P6"): 6.66666667,
    ("P4", "P7"): 8.33333333,
    ("P4", "P8"): 9.16666667,
    ("P4", "P9"): 11.66666667,
    ("P4", "P10"): 3.33333333,
    ("P5", "P6"): 4.71404521,
    ("P5", "P7"): 6.66666667,
    ("P5", "P8"): 7.50000000,
    ("P5", "P9"): 10.00000000,
    ("P5", "P10"): 8.04737854,
    ("P6", "P7"): 4.71404521,
    ("P6", "P8"): 5.77350269,
    ("P6", "P9"): 8.33333333,
    ("P6", "P10"): 10.00000000,
    ("P7", "P8"): 3.33333333,
    ("P7", "P9"): 6.66666667,
    ("P7", "P10"): 11.66666667,
    ("P8", "P9"): 5.77350269,
    ("P8", "P10"): 12.50000000,
    ("P9", "P10"): 15.00000000,
}


def get_travel_time(from_point: str, to_point: str) -> float:
    """
    Get travel time between two path points, considering bidirectional paths.
    
    Args:
        from_point: Starting path point (e.g., "P0")
        to_point: Destination path point (e.g., "P1")
        
    Returns:
        Travel time in seconds, or -1.0 if path not found
    """
    segment = (from_point, to_point)
    segment_reverse = (to_point, from_point)
    if segment in PATH_SEGMENT_TIMES:
        return PATH_SEGMENT_TIMES[segment]
    elif segment_reverse in PATH_SEGMENT_TIMES:
        return PATH_SEGMENT_TIMES[segment_reverse]
    else:
        return -1.0


def get_all_reachable_points(from_point: str) -> Dict[str, float]:
    """
    Get all points reachable from a given point with their travel times.
    This function considers paths to be bidirectional.
    
    Args:
        from_point: Starting path point
        
    Returns:
        Dictionary mapping destination points to travel times
    """
    reachable = {}
    for (start, end), time in PATH_SEGMENT_TIMES.items():
        if start == from_point:
            reachable[end] = time
        elif end == from_point:
            reachable[start] = time
    return reachable


def is_path_available(from_point: str, to_point: str) -> bool:
    """
    Check if a direct path exists between two points, considering bidirectional paths.
    
    Args:
        from_point: Starting path point
        to_point: Destination path point
        
    Returns:
        True if direct path exists, False otherwise
    """
    segment = (from_point, to_point)
    segment_reverse = (to_point, from_point)
    return segment in PATH_SEGMENT_TIMES or segment_reverse in PATH_SEGMENT_TIMES
