import os
from decimal import Decimal

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from config.partials import render_partial_or_full
from .services import CartService


def cart_view(request: HttpRequest) -> HttpResponse:
    cart = CartService.get_cart(request)
    items = list(cart.items.select_related('dish', 'dish__category').all()) if cart else []
    total_amount = cart.total_amount if cart else Decimal('0.00')
    total_quantity = cart.total_quantity if cart else 0

    min_order_amount = Decimal(os.getenv('MIN_ORDER_AMOUNT', '200'))
    meets_min_amount = (total_amount >= min_order_amount)
    min_diff = max(Decimal('0.00'), min_order_amount - total_amount)

    context = {
        'cart': cart,
        'items': items,
        'total_amount': total_amount,
        'total_quantity': total_quantity,
        'min_order_amount': min_order_amount,
        'meets_min_amount': meets_min_amount,
        'min_diff': min_diff,
    }

    if request.headers.get('HX-Request'):
        return render_partial_or_full(request, 'orders/cart.html#cart-content', context)

    return render_partial_or_full(request, 'orders/cart.html', context)


@require_POST
def cart_add_view(request: HttpRequest, dish_id: int) -> HttpResponse:
    quantity_raw = request.POST.get('quantity', '1')
    try:
        quantity = int(quantity_raw)
    except (ValueError, TypeError):
        quantity = 1

    option_id_raw = request.POST.get('option_id', '')
    option_id = int(option_id_raw) if option_id_raw.isdigit() else None

    CartService.add_dish(
        request,
        dish_id=dish_id,
        quantity=quantity,
        option_id=option_id,
    )

    if request.headers.get('HX-Request'):
        count = CartService.get_items_count(request)
        return render(request, 'partials/cart_badge.html', {'cart_items_count': count})

    referer = request.META.get('HTTP_REFERER')
    return redirect(referer or 'menu:catalog')


@require_POST
def cart_update_view(request: HttpRequest, item_id: int) -> HttpResponse:
    quantity_raw = request.POST.get('quantity', '1')
    try:
        quantity = int(quantity_raw)
    except (ValueError, TypeError):
        quantity = 1

    CartService.update_item_quantity(request, item_id=item_id, quantity=quantity)

    if request.headers.get('HX-Request'):
        return cart_view(request)

    return redirect('orders:cart')


@require_POST
def cart_remove_view(request: HttpRequest, item_id: int) -> HttpResponse:
    CartService.remove_item(request, item_id=item_id)

    if request.headers.get('HX-Request'):
        return cart_view(request)

    return redirect('orders:cart')


@require_POST
def cart_clear_view(request: HttpRequest) -> HttpResponse:
    CartService.clear_cart(request)

    if request.headers.get('HX-Request'):
        return cart_view(request)

    return redirect('orders:cart')


def checkout_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse('Checkout')

