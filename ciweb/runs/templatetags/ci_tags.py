from django import template

from ciweb.runs import fmt

register = template.Library()
register.filter('clock', fmt.clock)
register.filter('ago', fmt.ago)
register.filter('span', fmt.span)
register.filter('size', fmt.size)
register.filter('label', fmt.label)
