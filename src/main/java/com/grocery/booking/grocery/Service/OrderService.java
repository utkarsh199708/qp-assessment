package com.grocery.booking.grocery.Service;

import com.grocery.booking.grocery.Entity.Order;
import com.grocery.booking.grocery.Entity.OrderItem;
import com.grocery.booking.grocery.Repository.OrderRepository;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class OrderService {
    @Autowired
    private OrderRepository orderRepository;

    public Order createOrder(List<OrderItem> orderItems) {
        Order order = new Order();
        order.setItems(orderItems);
        return orderRepository.save(order);
    }

    // Other methods for retrieving orders, updating orders, etc.
}
