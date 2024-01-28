package com.grocery.booking.grocery.Controller;

import com.grocery.booking.grocery.Entity.Order;
import com.grocery.booking.grocery.Entity.OrderItem;
import com.grocery.booking.grocery.Service.OrderService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/user/order")
public class OrderController {
    @Autowired
    private OrderService orderService;

    @PostMapping
    public ResponseEntity<Order> createOrder(@RequestBody List<OrderItem> orderItems) {
        Order order = orderService.createOrder(orderItems);
        return new ResponseEntity<>(order, HttpStatus.CREATED);
    }

    // Other endpoints for retrieving orders, updating orders, etc.
}